# 40 字幕文本链

本文件是字幕文本步骤的**分步权威**。入口：`src/autoslice/producer_text_pipeline.py::run_text_pipeline`
（生产唯一调用方 `scripts/produce_slice_package.py`）。

## 阶段顺序（真实调用序）

| # | 子阶段 | 模块 | 作用 |
|---|---|---|---|
| 1 | `_collect_timeline_chat` | `producer_chat_input.py` | 弹幕/SC/礼物/上舰证据装载（XML 与显式绑定 JSONL 各守其权威） |
| 2 | `_transcribe_draft` | ASR 适配器 + `session_topic_authority.py` + `term_boundary.py` | 转写草稿 + 场级话题实体吸收 + 词边界统一 |
| 3 | `_build_entity_verification_context` | `read_aloud_llm_verifier.py`、`entity_audio_verifier.py` | 实体仲裁闭包（人工 override → 朗读 LLM → 音频仲裁） |
| 4 | `_apply_entity_authority` | `self_reference_absorption.py`、`chat_proposals.py`、`subtitle_fidelity.py`、`chat_repair.py` | 自称吸收、弹幕权威修复、数字事实门、音频实体落地 |
| 5 | `_run_final_review` | `final_review_auditor.py` | correction pass：发现问题、路由修复和逐条声学复核；产物为 `final-review-audit.v1`，不是放行回执 |
| 6 | `_finalize_text_evidence` | `subtitle_fidelity.py` 各 guard、`surface_canon.py`、`song_name_pin.py`、`source_subtitle_truth.py` | 语言保持/书名号/标点门、梗词定形、歌名钉、源真值投影（FAILED 即 SystemExit） |
| 7 | `review_final_boundary_semantics` | `producer_boundary_review_stage.py`、`boundary_semantic_review.py` | 对 resolver 前的 source full-window cue grid 评审四命题并保留 post-end witness，签发 `review_scope=source_full_window` 回执 |
| 8 | boundary resolver + `_materialize_final_recut` | `producer_boundary_resolution.py`、`producer_package_finalization.py` | snap/cut 后恢复最终边界对应的 reviewed baseline、重放 source truth 与其他 materialize authority，写出实际交付 SRT |
| 9 | `exact_delivery_correction_audit` | `producer_boundary_review_stage.py`、`boundary_semantic_review.py` | 从实际交付 SRT 重新解析 delivery-local grid，借 hash-bound source separation witness 复审并签发 `review_scope=final_delivery` 回执 |
| 10 | `_run_exact_final_release_review` / `_run_exact_final_review_gate` | `final_review_auditor.py`、`final_review_contract.py`、`producer_package_finalization.py` | 对 materialize 后的**精确最终 SRT raw bytes**重新发现问题，签发并按原始字节 SHA 校验 `final-review-audit.v2` |

语义修复引擎（专名/方言/语境不合适度）的设计与规则见
[41-semantic-repair.md](41-semantic-repair.md)——那是本步的核心子权威。

## 硬约束

- 上传语义修复只允许三类非 CPA mutation：Ivan operator truth、纯机械规范化和有完整
  `glossary-expected-value-gate.v1` 的高先验 canon。expected-value 只接受未登记近音误听面
  到登记 glossary/roster 词面；两边都是登记词面时专名平等，必须交 CPA。其余词面、语义、
  插入或删除变化都必须由 CPA 明确选择 `PROPOSED`；AGY/声学与拼音只作证据和冲突诊断，
  不拥有对 CPA 明确裁决的第二张否决票。
- glossary 中“一个明确 canonical + 明列误听面”的三字及以上变体自动进入零 CPA
  expected-value 表，并在所有 mutable 文本阶段之后重新规范化；括号中的事故日期/说明不是
  词面。两字日常词（如“小时/留下”）无条件替换的误伤先验过高，除非 profile 单独显式提升，
  否则仍交 CPA。这样“下斗里→沙豆李”可机械覆盖整片所有出现，而“专名A→专名B”仍被
  registered-term guard 拦截。
- 实体上下文构建完成后必须生成同一份 hash-bound `.clip-context.json`：绑定 candidate/date、
  官方源 SHA、整片 draft、selection hook、relation/topic、结构化弹幕/SC 与 scoped speech
  memory。终审、声学请求、StoryContract、record 和交付包只能引用验证过的同一 digest；
  payload、candidate、日期、源 hash 或 ledger hash 漂移立即阻断。
- `.clip-context.json` 必须保存未截断的整片 draft，硬上限 60,000 字；超过即阻断，不能用
  “前后各一段”伪装整片语境。给模型的 supplemental prompt 另有 18,000 字硬上限：它从整片
  cue、优先保留的 SC/礼物/上舰与按时间均匀采样的普通弹幕中做 cue-aware 选取，并显式标出
  omitted blocks。StoryContract 保存的 `clip_context_prompt` 必须由当前 sidecar 重新渲染后
  逐字相等；boundary/final reviewer 必须收到这份 hash-bound prompt 的完整字节，不能再把
  合法的 18,000 字输入静默截成 12,000 字。超过 18,000 字必须以
  `CLIP_CONTEXT_PROMPT_BUDGET_EXCEEDED` /
  `BOUNDARY_SEMANTIC_REVIEW_CANDIDATE_CONTEXT_OVERFLOW` 阻断；context、预算或 renderer 漂移
  一律 `CLIP_CONTEXT_PROMPT_BINDING_DRIFT`。
- 话题图只负责把当前日期/作品/活动节点和其子实体缩成候选闭集：
  `topic-resolution.v1` 必须披露 `RESOLVED`、`NO_MATCH`、`AMBIGUOUS`、`NO_GRAPH`、
  `GRAPH_EXPIRED` 或 `GRAPH_INVALID` 及 graph SHA（若已读取）。它不能直接授权改字；最终
  专名仍须音频、画面、结构化聊天或 source truth 见证。topic resolution 与 scoped graph
  context 一并进入 clip-context digest，不能在终审后偷换。
- “语境”默认是**整个切片和当前场次**，不是争议 cue 前后几句。clip-context 必须让审片员
  看见片内开头到结尾的 callback/复述/调侃链，也可携带与该日期和话题直接相关的结构化
  直播标题、联动对象、游戏/活动/公告实体；这些只能扩大候选与解释空间，不能在没有音频/
  画面/弹幕/source truth 见证时直接改字。前句说“姐感的妹妹”、后句拿同一句调侃，属于同一
  语义链；逐 cue 独立校正会丢掉这种证据，禁止作为生产默认。
- speech memory 只生成候选闭集，`mutation_authorized=false`；必须按 candidate/relation/date
  scope 检索并携带 `candidate_memory_id`。它不能冒充 source_surface，不能进入 glossary，
  即使与误听同音也必须走声学仲裁。片内另一个由同一 ASR 派生的 cue 同样只是相关候选，
  不得作为独立文字证人直接改字。上下文展示用的 `id=` 不是 ID 本体；终审只可在去掉
  **一个**该固定展示前缀后精确命中哈希绑定 ledger 时受控规范化，未知 ID 禁止模糊匹配。
- 音频二听只证明读音，不证明任何同音/近同音/字母写法；人名形态守卫还会特别检查带
  「小/老/阿」前缀或「神/老师/姐/哥/酱/桑/君/总/宝」后缀的跨度。没有文字权威就只回退
  该换字跨度，同 cue 其余有见证修复仍保留。守卫同时检查 draft 改写跨度本身的人名形态，
  并只额外容忍 `-n/-ng` 鼻音尾漂移来识别近同音（如 `毁神→绘声`）；不得因改写把「神」
  一起吃掉就逃过相邻后缀检查。`什么/怎么/为什么/谁/哪里/多少` 等疑问意图族发生变化则
  整 cue 回退，禁止把逐字字幕改成解释性提问。
- 字母昵称的规范词面与口播读音必须分层：已有 source-backed entity provenance、建议包含
  字母、且**整条 current/proposed 的去标点拼音在折叠相邻口语重启后完全相同**时，声学层
  听到字母名（如 `N→恩`）不得以 grapheme 不同否决 `大N`。该窄门不提供 provenance，
  不适用于普通语义改写、未知专名或发音不等价候选。
- 源真值支持 `replace_cue` / `replace_substring` / `drop_cue`；`drop_cue` 只允许删除被 source-timeline 真值半开区间完整包含的 cue（仅容忍 120ms 编码/SRT 边界漂移）。任何实质性跨界均记 `DROP_CUE_STRADDLES_TRUTH_INTERVAL` 并 fail closed，禁止按“有重叠”整条删除。
- boundary semantic receipt 分两层。resolver 前的 `source_full_window` 回执只能绑定当时完整
  source grid，并用 endpoint 后 cue 证明下一话题；它不是最终交付字幕回执。resolver 与
  `_materialize_final_recut` 完成全部实际交付改写后，必须从精确最终 SRT 重新解析 grid 并签发
  独立的 `final_delivery` 回执。后者可用
  `talk-boundary-source-separation-witness.v1` 继承 source 层的 post-end 分离证明，但仍须按
  当前最终文本重新判断 syntax/story。两层 request、cue ordinal、grid SHA 与坐标分别绑定，
  不得要求相等，也不得因 endpoint ms/text 碰巧相同而平移复用。
- 源真值的 `local_windows` 是容忍 fresh-ASR 时间漂移的**发现窗口**，不是最终 cue ownership。
  在实体仲裁与 `_run_final_review` 前，流水线先对当前 draft 做确定性 source-truth preview：
  `applied/satisfied` row 只按校验通过的
  `source-truth-resolved-target-projection.v1` 精确 cue index 进入保护集；宽窗仅擦到的邻 cue
  不得被豁免。只有仍失败的 `required=true` truth 才可在 preview 中退回原始窗口作保守保护，
  并须留下 typed unresolved-fallback receipt；`required:false` 不得成为最终 owner。源真值
  随后仍在正式阶段重放并复验，因此 preview 不是“提前应用后跳过验证”，而是禁止低权威阶段
  抢写已确定的最高权威目标。projection 缺失/非法、cue index/timing 不一致或 required truth
  最终未满足均 fail closed。
- 两条 required `IVAN_OPERATOR_TRUTH / replace_cue` 在同一源录像上首尾精确相接，而 fresh
  ASR 的一条 cue 跨过该公共边界时，禁止让后写 truth 复用并覆盖前写 owner。流水线只在两边
  源区间均完整保留、前窗唯一拥有骑界 cue、后条审定文本可唯一拆成“新前缀 + 已正确后缀”时，
  按绝对源边界拆分 cue，并写 `source-truth-adjacent-cue-partition.v1` 收据；preview 把拆后
  owner 映回原 cue 保护，正式落地保留拆后精确时间轴。任何不唯一或后缀漂移都 fail closed。
- 宽 `replace_substring` 窗若同一专名出现多次，必须用 `mention_postconditions` 为每一次
  绑定绝对 source interval、required text 与 forbidden tokens；窗口内“某一次写对”不能
  掩盖另一 mention 仍错误。全部 mention 必须先独立解析、隔离并通过；这些 mention 对应 cue
  的并集同时是**实际 mutation target**和最终 exact owner projection。即使 fresh ASR 已经写对、
  本轮没有发生 replacement，也不得退回宽 `local_windows` 或“所有含 required text 的 cue”
  取得 ownership。fresh ASR 把两个 mention 合进同一 cue、任一 mention 缺失或无法分别归因时，
  必须在改字前 fail closed；未审的父窗口 cue 绝不能先被改写后再从审计 projection 中消失。
- `replace_cue` 可附带经人工/黑屏纯音频听证确认的绝对源时间轴 `spoken_start_ms`：用于删除幻听前缀后把保留口播的字幕起点同步收紧。目标必须唯一；真值宽窗擦到的前句仅在其结束早于审定起点时排除，fresh ASR 的目标 cue 起点最多可比审定起点晚 500ms（随后回钉到绝对起点），若仍有后续重叠 cue、前句跨过起点、越界或非整型则 fail closed。VAD 未检出本身仍不得推导这个起点。
- 已审字幕是独立于封面的文本权威。候选级资产放在 profile 的 `reviewed_subtitle_baselines`
  目录，由 runner 自动发现并写入候选指纹/spec；不得再以 `--reuse-cover` 作为是否保留人工字幕的
  条件。`subtitle-redelivery-baseline.v2` 同时绑定 SRT 哈希、源录像 basename/SHA-256 与绝对
  source coverage：本轮 BCUT 时间保留，文本按绝对源时间逐 cue 恢复旧版，再统一重放更高权威
  的全部源真值；新切点可在干净 cue 边界裁短或扩展，未审扩展区明确记账。哈希/源 identity
  漂移、覆盖边界切半 cue、漏 cue、合并/拆分、歧义映射或二次真值失败一律拒发，禁止靠重掷
  模型碰运气。只有已由 `drop_cue` 删除、无法与旧稿一一配对的静音窗会从两边同时排除；不能
  因宽真值窗内“任一 cue 已出现 required_text”就掩盖同窗其他新误听。
- redelivery v2 在 coverage prefix/tail 唯一允许保留的 edge straddler，必须与**每一条**
  retained reviewed cue 都按半开区间零重叠；恰好边界相接的 0ms overlap 可披露为
  `BOUNDARY_STRADDLE_WITHOUT_REVIEWED_CUE_OVERLAP`。任何正重叠，包括 1ms，仍须报
  `REDELIVERY_CURRENT_CUE_STRADDLES_REVIEWED_COVERAGE` 并拒发，不能把“边缘 cue”当宽松豁免。
- v2 manifest 只有显式声明 `exact_interval_replay=true`，且源文件 basename、SHA-256、绝对
  起止区间全部逐字相同时，整份人工审定 SRT（含 cue 时间）才可直接重放。这防止同源重跑因
  ASR 随机漏 cue 而删除已审字幕；随后仍必须重放 source-truth。只要区间发生裁切或扩展，就
  回到上面的逐 cue 绝对时间映射，缺失、合并、拆分或漂移继续 fail closed，不能把审定时间轴
  宽松套用到另一段素材。fresh cue 形状导致的
  `replace_cue / REPLACE_CUE_TARGET_NOT_UNIQUE` 只能在这条 exact 路径延后。另一个同样窄的
  例外是 required `replace_substring` 的 mention postcondition：只在每个 failure 都是
  `MENTION_REQUIRED_TEXT_MISSING` 或 `MENTION_FORBIDDEN_TOKEN_SURVIVED`、`local_windows`
  非空、`mention_owner_resolution.status=PASS`，且 v2 source binding 完整有效时，才可延后到
  exact replay；缺 mention、无法隔离、owner BLOCK、混合 failure、timing pin 或无 exact
  authority 仍立即阻断。finalizer 还必须证明实际 replay/restore authority 有效，并把所有仍
  与最终交付区间重叠的 deferred `truth_id` 在重放后的 `applied+satisfied` 中逐个复证，不能只
  看总状态非 FAILED。完全位于最终交付区间外的 deferred truth 不应在裁掉后的成片中复现，
  但必须以 `context_only_truth_ids` 和逐窗 interval evidence 明示排除；跨过终点或同时包含
  inside/outside windows 的 truth 仍须阻断。边界角色与半开区间定义见
  [30-boundary.md](30-boundary.md)。
- 最终裁决顺序固定为：**先按最终边界恢复 reviewed baseline → 再重放更高权威 source truth
  → 对每个 baseline mapping 与 source-truth declared output 在最终 clean SRT 和 speaker SRT
  上逐项验活 → 才允许低权威 repair 记为 `SUPERSEDED_*`**。owner 自己未通过时，不能用
  “低权威项已被覆盖”制造 `final_required_decision_count=0` 的假绿。审计字段
  `final_source_truth_owner_verification` 与
  `final_redelivery_baseline_owner_verification` 在对应 owner 存在时必须为 PASS，且该类
  required count 非零。
- required source truth 仍在完整 padded context 上应用，但 boundary owner 资格只属于完整
  落在 candidate-relative immutable story scope 的 truth；该 scope 仅在开场容忍并冻结
  `semantic_start` 前最多 500ms 的 cue 时间抖动，使完整开场 cue 可把最终 start 拉回自身
  起点。更早 lead/post context truth 修字但不抬高边界，超过容差的开场跨界、尾部跨界及其他
  scope straddle 均 fail closed。具备对应 typed ownership contract 的 applied
  story-chat owner 也须完整落在同一 scope，随后在裁切前冻结并由最终边界完整覆盖。
  `boundary_role=next_topic_witness` 只负责证明分离，必须以 context-only 留证，不得取得
  boundary owner。reviewed baseline 仍须在最终 clean/speaker SRT 逐 mapping 验活，但它是文字
  权威，不进入 boundary owner 列表，不能冻结旧切片尾部。整句 `exact_read`
  必须由 whole-line gate 明示 `owner_eligible=true`；sender/gift/coreference/entity 等窄槽
  则按各自 slot contract，不借用整句字段。`required:false` truth、partial/proxy chat support
  与 context-only verdict 不能进入 owner 列表。finalizer 发现任一真实 owner 被裁掉或只剩
  残片时必须记
  `BOUNDARY_REQUIRED_OWNER_EXCLUDED` 并拒发，不能因成片外已“不可见”就把它降级为
  `NOT_REQUIRED` / `OUTSIDE_DELIVERY`。完整边界契约见 [30-boundary.md](30-boundary.md)。
- 两条相邻 required `IVAN_OPERATOR_TRUTH / replace_cue` 的共同源边界若落进同一个 fresh
  ASR cue，先按该绝对源边界拆 cue，再把后一条人工真值按其**完整精确所有区间**重分到新
  prefix 与后续 cue；不得要求后续 ASR 文本碰巧已经等于拆分后缀，也不得把 ASR 重复带入
  成片。该窄路只在前后真值区间完整保留、目标 cue 连续且两端与人工区间精确对齐时启用，
  否则 fail closed。
- final owner verifier 以 resolver 的最终半开区间
  `[delivery_start_ms, delivery_end_ms)` 重新分类全部 required source truth：完全在成片外的
  任意 truth（不只 `next_topic_witness`）必须显式记为 context-only；完全在成片内的 truth
  必须在 clean/speaker SRT 上逐窗验活；跨过任一终点或同一 truth 同时含 inside/outside
  windows 一律 fail closed。这里的最终可见性分类不反向授予成片外 truth 边界 ownership。
- 已登记 source alias 的结构化聊天必须显式绑定：官方源 basename/SHA-256、canonical sidecar
  path/SHA-256、JSONL 自身 origin epoch、alias timeline offset 与 `source_alias_id` 缺一不可；
  JSONL 的事件时钟不得从另一份官方媒体 basename 猜。已知 alias 但 sidecar 缺失、哈希漂移、
  无可解析事件时必须阻断，不能退化成误导性的 `evidence_considered=0 / NO_MATCH`。仅没有 alias
  authority 的旧录播可显式 `structured_chat_required=false`。`GUARD_BUY` 是独立 `guard`
  证据，按 username/uid/guard level 装载，并使用 300 秒上舰答谢因果窗；多事件无法唯一对应时
  保留原字幕而非猜名。
- 结构化 SC 跨 cue 对齐时，只有 SC 从开头到当前 internal gap 的**完整规范化前缀**逐字包含
  在上一 cue，才可声明该前缀由上一 cue ownership 并从当前 span 去重。`0.8` fuzzy coverage
  只能辅助判断 gap 是否曾读过，不能替代完整前缀 exact containment；少了 `不/不是/没` 等
  极性词时必须拒绝 rebase，禁止用高相似度把反向语义当成重复前缀丢掉。
- 结构化 SC/弹幕整句复制必须另过 typed whole-line support gate。gate 要逐项保存每一路
  support 的 score、coverage、precision、匹配范围和 unsupported head/interior/tail；主
  transcript fuzzy 命中、partial span、context-only audio verdict 或只见证实体槽都不能把
  未说出的前后缀补进字幕。只有 full-span hash-bound raw audio、owner-eligible 的近完整独立
  transcript，或现行明示 strong-thread-anchor 窄例外，才可令 applied row
  `owner_eligible=true`；失败时整句保持原口播，仅允许已独立见证的 entity/source-truth 槽位
  修复。
- whole-line 结构检查判定 head/tail 支持前必须剥离**边界借字**：authority 边界侧 ≤2 字的
  孤立匹配块，若与相邻匹配块之间隔着 ≥3 字的 observed 侧插入 run，视为从转录相邻句借来的
  同形字（剥离结果披露在 `borrowed_boundary_blocks_stripped`）。1863 实案：SC 尾字「了」
  她没念，独立转录连写到下一句「哎，现在几点了」，子序列对齐借同形「了」伪造出 near-complete
  逐字朗读，SC 整行改写 applied 后又被 redelivery baseline 拉回，终验对该 exact_read 快照
  永久失配。非逐字朗读（加字/漏字）一律保持已审口播文本，不注入 SC 原文。
- 书名号结构门在所有文本 authority（含源真值）之后再跑一次；合法跨 cue 配对单独记账，真正的 `UNRESOLVED_COMPLEX_IMBALANCE` 必须阻断 `review_ready`。
- 最终 clean/speaker SRT 在 burn 前必须经过
  `src/autoslice/subtitle_validation.py::validate_srt_file`：每个非空 block 都必须被消费，
  cue 编号连续、时间戳合法、`end > start`、最短 300ms、单调且无重叠、文本非空、不以孤立
  标点或单个汉字充当 cue、不得越过媒体尾部。解析器静默跳过坏 block 一律视为失败。
  package audit 与 authorized upload 会各自重新运行同一 validator，不能信 producer 自报。
- `final-review-audit.v1` 只描述 correction pass 的发现、路由与修复结果；即使它显示
  `CLEAN`/`APPLIED`，也不能证明后续 source truth、baseline 或 finalizer 没有引入回归。
  放行只认 `final-review-audit.v2`：它的 `reviewed_srt_sha256` 必须绑定包内 SRT 的原始
  `read_bytes()`，不得先按文本模式或换行符规范化；discovery 明确
  `COMPLETE`，`findings` 是合法列表且 validated count 精确相等，状态 `CLEAN`、
  `release_gate=PASS`、零 finding，并携带 PASS 的 correction-mutation audit、`final_delivery`
  boundary semantic review、source separation witness 与 delivery-local endpoint binding。
  source `source_full_window` 回执仍须独立保留在 boundary audit，package 再重算 witness 对它的
  规范 SHA 绑定；它不能塞进 v2 冒充最终回执。provider/JSON 失败、缺失或 null/non-list
  findings、全部 finding 无效、任何剩余 finding、raw-byte SRT hash 漂移或任一 typed receipt
  非 PASS 都阻断。
- correction pass 对 SRT 与 chat authority audit 是原子事务：所有 mutation 先 staged；任一
  discovery/routing/provider 异常必须恢复原始 SRT 字节且不提交 staged audit，并输出 typed
  `final-review-audit.v1 status=AUDITOR_UNAVAILABLE`、`release_gate=BLOCK`、原始
  `reason_code/detail`、`findings=[]`、`applied_count=0`。exact-final 后续空 rescan 不能洗白
  该失败；mutation audit 必须报 `CORRECTION_DISCOVERY_INCOMPLETE`。只有 typed
  `AUDITOR_UNAVAILABLE` 可按有界
  `provider_transient / final_review_correction_discovery` 重试；非法合同/状态仍是终态错误。
- exact-final 中 AGY/声学层是证人，不是法官：它只给出目标是否可闻、疑似拼音及
  current/proposed 发音兼容度，CPA 结合文字 provenance 与整片语境作最终
  CURRENT/PROPOSED 裁决。代码必须记录拼音/不可闻证据与 CPA 选择的冲突，但不得用 AGY
  或兼容度阈值推翻 CPA 明确的 `PROPOSED`；CPA 未明确选边或调用失败才是未决。声音不能
  单独选择两个同音正字法；同音或规范发音键相同（如 `大恩→大N`）须有绑定文字证据，或
  满足可重算的严格同音闭集并由 CPA 明确作语义 tie-break，否则记录
  `ORTHOGRAPHY_NOT_DECIDABLE_FROM_AUDIO` 并阻断。
- exact-final SRT 使用**交付局部时间轴**，而声学 verifier 绑定的通常是带前后 padding 的源
  media。每个 `subtitle-span-acoustic-check-request.v1` 必须显式携带非负
  `source_media_timeline_offset_ms`，并把它纳入 evidence/request hash；实际裁剪必须执行
  `source_media_ms = delivery_local_ms + offset`。verdict/manifest 必须以
  `subtitle-audio-timeline-binding.v1` 同时记录 delivery-local target/context 和 source-media
  target/crop。字段缺失、负数、类型错误、请求重算 hash 不符、target 越界或旧 cache 未绑定
  offset 都 fail closed；branding intro 不参与这个 pre-burn 时间轴。
- correction pass 的同音/近同音/字母 mutation 必须携带 CPA `PROPOSED`，或同时携带可重算的
  `glossary-expected-value-gate.v1` 与 `expected-value-canon-authority.v1`。后者要求 glossary/
  official roster provenance、拼音相容、current 未登记、proposed 已登记；两边已登记立即失效。
  raw glossary prose、纯 acoustic、同片 transcript recurrence、宽泛 context 和 speech-memory
  只能召回。Ivan operator truth 另由 governed late source-truth 精确绑定。
- `final-review-audit.v2` 必须携带 PASS 的
  `subtitle-correction-mutation-audit.v1`，把 correction pass 的 `applied_count` 与所有实际
  applied mutation 逐条对齐，并验证每条 typed authority receipt。缺回执或计数漂移均报
  `FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID`；第二遍 exact discovery 即使返回空
  findings，也不能洗白第一遍已经发生的无权 mutation。
- exact-final 扫描只审最终字节，不在同一轮直接改字；但 CPA 已明确 `PROPOSED` 的 finding
  必须写入 `final-review-carryover.v1`，并由下一轮 correction pass 走同一套裁决/落字门。
  runner 只有在 chat audit 声明计数、sidecar schema/行数，以及每条
  `(cue, suspect, proposed_full_cue)` 与 exact 审计中的 `repaired=true` finding 全部一致时，
  才把该失败列为 recoverable；每个新的 failure fingerprint 自动获得恰好一次下一轮
  correction pass，消费过的同一 fingerprint 不得再次自旋，单候选最多消费 8 个不同
  carryover fingerprint。correction 或 exact discovery 为 `AUDITOR_UNAVAILABLE` /
  `CORRECTION_DISCOVERY_INCOMPLETE` 时，不得把空 findings 当 clean 而删除未消费的旧
  sidecar；无新行就原字节保留，有新行则按 `(cue, suspect, proposed_full_cue)` 合并
  去重。只有 discovery 完整时才允许以本轮 exact 结果替换或清空 sidecar。缺文件、
  计数漂移或内容不符仍 terminal fail closed。normalized finding 写入 raw carryover 时必须把
  `suggestion → replacement`，并把 glossary/roster `candidate_provenance.surface →
  source_surface`；尤其 `suspect=""` 的零长度专名插入不能丢掉这两项，否则下一轮会把已由
  CPA 定案的高先验规范词误判为无 provenance，形成永久重试。
- 幻听删除是一等声学动作：局部无声前缀用 `acoustic_delete`，只有“保留后的完整 cue =
  SUPPORTED 且原 cue = INCOMPATIBLE”才应用；整 cue 只有 `target_audible=false` 才可
  `acoustic_drop_cue`。局部静音绝不授权删除后半段真实口播；不确定时保留/留空并阻断，
  不为语句顺滑补词。语义校正模型漏掉 cue 或返回空 cue **不构成**删除证据：fidelity 层必须
  恢复 draft 并记 `CUE_DELETION_REQUIRES_ACOUSTIC_AUTHORITY`；即使没有第二路 ASR 也不能
  静默删除，有同时间键 AGY/独立听写非空时还要把该反证写入审计。
- source-language 门区分“模型凭空引入外语口播”与“高权威专名含外文字形”。只有
  `VERIFIED_ACTIVE` 且 entry hash 合法的 source-truth 声明输出可以正向见证其精确 kana run；
  不能从整条 post-edit `after` 循环自证。`replace_substring` 仅在 canonical 实际应用，或显式
  `required_text` postcondition 已满足时可见证；部分窗口、部分 surface、generic redelivery
  baseline 继续拒发。
- 逐字取自本候选**已绑定结构化弹幕记录**的 sender / gift 名（`clip_context.structured_chat`）
  同样正向见证其自身 kana，见证类型 `structured_chat_name`。用户名的字形归平台记录所有，
  不由主播读音决定——她用中文腔念日文假名 ID 是常态，音频 `kana_similarity=0` 不构成反证
  （实例：2026-07-22 `auto_193450_1573_1672` cue76 `梅杰克家的六更るり`）。该豁免精确且完全：
  cue 内**每一个**假名都必须落在这类名字里，名字旁边掺入任何臆造日语仍 fail-closed。此门
  正是为了让"原版弹幕名字必须复制过来"成立，不得反过来惩罚正确复制。
- source-language 整 cue 回退只适用于无中文的 Latin-language cue；中文口播里的 NN/L、NNLL、
  LLNNHHB 等 CP 顺序公式以及大写 `TA` 代词是标签/中文代词，不是外语段落，不得触发
  mixed-language 拒发，也不得因 token 数下降把已删除的跨 cue 回声整句恢复。
  `_SAFE_CODE_SWITCH_WORDS` 只登记已有多路转写证据支持、在中文口播中作为普通借词使用的
  词项（例如技术语境的 `staff`、`bug`）；它不是整句外语白名单，未登记的多词 Latin 组合
  仍须精确音频见证或更高文本权威。
- 交付 `.srt`/`.ass` 走内容时间轴；片头偏移只记录在 `burned_preview.branding_intro.intro_offset_ms`（见 [80-package-delivery.md](80-package-delivery.md)）。
- talk 成品 `speaker_mode=required`。说话人未决或证据不足进入
  `speaker_review_required` / `speaker_evidence_insufficient` 并 fail closed；不得为了
  “统一李豆沙色”把不确定来宾涂成主播后放行。歌切不进入 talk speaker 链。
