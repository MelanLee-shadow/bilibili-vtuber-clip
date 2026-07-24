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
| 7 | `_run_exact_final_release_review` | `final_review_auditor.py` + `final_review_contract.py` | 对所有 authority 落地后的**精确最终 SRT 字节**重新发现问题，签发 hash-bound `final-review-audit.v2` 放行回执 |

语义修复引擎（专名/方言/语境不合适度）的设计与规则见
[41-semantic-repair.md](41-semantic-repair.md)——那是本步的核心子权威。

## 硬约束

- 上传语义修复永不放行未见证改写：可听辨的发音/语义变化须过相应 fidelity/声学门；同音、
  近同音或字母正字法变化还必须有 cue/referent-bound typed textual authority。拼音相同、
  纯音频、同片 transcript 或宽泛 context 只能生成候选，不能单独授权选字；缺权威就 revert
  并阻断。
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
- hash-bound 源真值拥有的 cue 在 `_run_final_review` 前即进入保护集，终审不得先把中文音译改成
  假名、也不得用声学对同音专名重新选字后再指望末尾钉子挽救。保护只容忍总计 `<=250ms`
  且不超过 cue 10% 的**外边界**漂移；区间内部有空洞仍 fail closed。源真值随后照常重放并复验，
  因此保护不是“跳过修复”，而是禁止低权威阶段抢写最高权威辖区。
- 宽 `replace_substring` 窗若同一专名出现多次，必须用 `mention_postconditions` 为每一次
  绑定绝对 source interval、required text 与 forbidden tokens；窗口内“某一次写对”不能
  掩盖另一 mention 仍错误。fresh ASR 把两个 mention 合进同一 cue 而无法分别归因时，
  `MENTION_POSTCONDITION_TARGET_NOT_ISOLATED` fail closed，禁止假阳性通过。
- `replace_cue` 可附带经人工/黑屏纯音频听证确认的绝对源时间轴 `spoken_start_ms`：用于删除幻听前缀后把保留口播的字幕起点同步收紧。目标必须唯一；真值宽窗擦到的前句仅在其结束早于审定起点时排除，fresh ASR 的目标 cue 起点最多可比审定起点晚 500ms（随后回钉到绝对起点），若仍有后续重叠 cue、前句跨过起点、越界或非整型则 fail closed。VAD 未检出本身仍不得推导这个起点。
- 已审字幕是独立于封面的文本权威。候选级资产放在 profile 的 `reviewed_subtitle_baselines`
  目录，由 runner 自动发现并写入候选指纹/spec；不得再以 `--reuse-cover` 作为是否保留人工字幕的
  条件。`subtitle-redelivery-baseline.v2` 同时绑定 SRT 哈希、源录像 basename/SHA-256 与绝对
  source coverage：本轮 BCUT 时间保留，文本按绝对源时间逐 cue 恢复旧版，再统一重放更高权威
  的全部源真值；新切点可在干净 cue 边界裁短或扩展，未审扩展区明确记账。哈希/源 identity
  漂移、覆盖边界切半 cue、漏 cue、合并/拆分、歧义映射或二次真值失败一律拒发，禁止靠重掷
  模型碰运气。只有已由 `drop_cue` 删除、无法与旧稿一一配对的静音窗会从两边同时排除；不能
  因宽真值窗内“任一 cue 已出现 required_text”就掩盖同窗其他新误听。
- v2 manifest 只有显式声明 `exact_interval_replay=true`，且源文件 basename、SHA-256、绝对
  起止区间全部逐字相同时，整份人工审定 SRT（含 cue 时间）才可直接重放。这防止同源重跑因
  ASR 随机漏 cue 而删除已审字幕；随后仍必须重放 source-truth。只要区间发生裁切或扩展，就
  回到上面的逐 cue 绝对时间映射，缺失、合并、拆分或漂移继续 fail closed，不能把审定时间轴
  宽松套用到另一段素材。fresh cue 形状导致的 `replace_cue / REPLACE_CUE_TARGET_NOT_UNIQUE`
  只能在这条 exact 路径延后；结构冲突、timing pin、postcondition 等失败仍立即阻断。finalizer
  还必须证明实际策略确为 `exact_reviewed_interval_replay`，并把每个延后 `truth_id` 在重放后的
  `applied+satisfied` 中逐个复证，不能只看总状态非 FAILED。
- 最终裁决顺序固定为：**先按最终边界恢复 reviewed baseline → 再重放更高权威 source truth
  → 对每个 baseline mapping 与 source-truth declared output 在最终 clean SRT 和 speaker SRT
  上逐项验活 → 才允许低权威 repair 记为 `SUPERSEDED_*`**。owner 自己未通过时，不能用
  “低权威项已被覆盖”制造 `final_required_decision_count=0` 的假绿。审计字段
  `final_source_truth_owner_verification` 与
  `final_redelivery_baseline_owner_verification` 在对应 owner 存在时必须为 PASS，且该类
  required count 非零。
- 所有 required source-truth/baseline/story-chat owner 还必须在裁切前冻结并由最终边界完整
  覆盖；finalizer 发现任一 owner 被裁掉或只剩残片时必须记
  `BOUNDARY_REQUIRED_OWNER_EXCLUDED` 并拒发，不能因成片外已“不可见”就把它降级为
  `NOT_REQUIRED` / `OUTSIDE_DELIVERY`。完整边界契约见 [30-boundary.md](30-boundary.md)。
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
- 书名号结构门在所有文本 authority（含源真值）之后再跑一次；合法跨 cue 配对单独记账，真正的 `UNRESOLVED_COMPLEX_IMBALANCE` 必须阻断 `review_ready`。
- 最终 clean/speaker SRT 在 burn 前必须经过
  `src/autoslice/subtitle_validation.py::validate_srt_file`：每个非空 block 都必须被消费，
  cue 编号连续、时间戳合法、`end > start`、最短 300ms、单调且无重叠、文本非空、不以孤立
  标点或单个汉字充当 cue、不得越过媒体尾部。解析器静默跳过坏 block 一律视为失败。
  package audit 与 authorized upload 会各自重新运行同一 validator，不能信 producer 自报。
- `final-review-audit.v1` 只描述 correction pass 的发现、路由与修复结果；即使它显示
  `CLEAN`/`APPLIED`，也不能证明后续 source truth、baseline 或 finalizer 没有引入回归。
  放行只认 `final-review-audit.v2`：它必须绑定最终 SRT SHA-256，discovery 明确
  `COMPLETE`，`findings` 是合法列表且 validated count 精确相等，状态 `CLEAN`、
  `release_gate=PASS`、零 finding，并携带 PASS 的 correction-mutation audit、boundary
  semantic review 与 final endpoint binding。provider/JSON 失败、缺失或 null/non-list
  findings、全部 finding 无效、任何剩余 finding、SRT hash 漂移或任一 typed receipt 非 PASS
  都阻断。
- exact-final 的声学复核只能关闭“当前读音支持且建议读音明确不兼容”的可听辨提案。若 finding
  涉及同音、近同音、`repair_class=phonetic`、字母规范写法，或 current/proposed 的规范化
  发音键相同（如 `毁神→绘声`、`大恩→大N`），纯音频不能决定字形；即使 verdict 报
  current `SUPPORTED`、proposed `INCOMPATIBLE`，仍必须保留 finding、记录
  `ORTHOGRAPHY_NOT_DECIDABLE_FROM_AUDIO` 并阻断 v2 放行，直到独立文字权威解决。
- correction pass 中所有已应用的同音/近同音/字母正字法 mutation，都必须携带与当前 cue
  或 referent 精确绑定且 PASS 的 `subtitle-orthography-authority.v1` 文字权威回执。可授权的
  typed provenance 仅包括 glossary、official roster、source truth、bound structured chat、
  verified OCR，或另有强制层已显式标记 `mutation_authorized=true` 的来源。纯 acoustic、
  同片 transcript recurrence、宽泛 `structured_context`（含 selection hook）和 speech-memory
  命中都只能提出 candidate，不能授权选字。
- `final-review-audit.v2` 必须携带 PASS 的
  `subtitle-correction-mutation-audit.v1`，把 correction pass 的 `applied_count` 与所有实际
  applied mutation 逐条对齐，并验证每条 typed authority receipt。缺回执或计数漂移均报
  `FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID`；第二遍 exact discovery 即使返回空
  findings，也不能洗白第一遍已经发生的无权 mutation。
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
- source-language 整 cue 回退只适用于无中文的 Latin-language cue；中文口播里的 NN/L、NNLL、LLNNHHB 等 CP 顺序公式以及大写 `TA` 代词是标签/中文代词，不是外语段落，不得触发 mixed-language 拒发，也不得因 token 数下降把已删除的跨 cue 回声整句恢复。
- 交付 `.srt`/`.ass` 走内容时间轴；片头偏移只记录在 `burned_preview.branding_intro.intro_offset_ms`（见 [80-package-delivery.md](80-package-delivery.md)）。
- talk 成品 `speaker_mode=required`。说话人未决或证据不足进入
  `speaker_review_required` / `speaker_evidence_insufficient` 并 fail closed；不得为了
  “统一李豆沙色”把不确定来宾涂成主播后放行。歌切不进入 talk speaker 链。
