# 30 边界解析

本文件是边界步骤的**分步权威**。

- 候选是内容锚点不是最终边界；最终起止由源语境证据 + 边界解析器决定（AGENTS.md 项目方向）。
- 链路：`source_context_planner.py`（计划扩窗）→ `source_context_executor.py`（执行）→ `boundary_resolver.py` / `producer_boundary_resolution.py`（定稿）→ `live_source_review.py`（含 song_boundary 专线）。
- `semantic_start/end` 对准真实语音起止读 `padded.fresh.srt`；候选 start 包含必要前置铺垫。
- 边界红旗 → quarantine（runner 规则），不许带伤交付。
- CPA 判官的 `context_expand_before/after_ms` 扩窗建议在本步消费（自动扩窗后重审）。
- 为保护开头音素而保留的 pre-roll 可以有声无字；若上一 cue 只因裁切重叠而露出
  `<=300ms` 的不可读字幕残片，保留音频但删除该闪字。该判断必须发生在最短可读时长
  延展之前；即使上游已把 cue 预裁到恰好从成片 `0ms` 开始，也按同一规则处理，不能把
  250ms 的上题尾巴延展成 1s 的醒目假开场。超过此阈值的实质内容不得靠这条规则吞掉。

## 四命题边界门

talk 成片的最终 end 必须同时成立：

1. 候选内容锚点全部覆盖，不能为求句尾提前删掉已选内容；
2. 句法完整，`cue end`、标点或静音都不能单独证明一句说完；
3. 故事/回答/包袱已经落地；
4. 下一 cue 已被证明是下一条 SC、谢礼或另一话题，不能吞进本片。

普通 hash-bound 人工 end（`boundary_end_mode=semantic_lower_bound`，也包括未显式声明 mode
的普通 `given_end_ms`）只表示“人工已确认至少要保留到这里”的**下界**，不是可绕过语义门的
绝对截断点。它不得早于候选 `end_ms`，不得砍掉 content anchor，也不得覆盖一个更晚的语义
闭环建议。这里必须区分两种量：`semantic closure cue end` 是 reviewer 选择的句尾，
`delivery coverage lower bound` 是最终媒体至少覆盖到的位置。若 coverage lower bound
恰好落在该 closure cue 后固定 400ms 尾气内，可保留 reviewer 选择的 cue，并由
`adaptive_tail_cut` 生成尾气；不得为了满足媒体覆盖下界而吞进下一句。实际尾气若被下一
cue/VAD guard 钳到下界之前，仍以 `BOUNDARY_REQUIRED_OWNER_EXCLUDED` /
`BOUNDARY_DELIVERY_LOWER_BOUND_EXCLUDED` 阻断，不能靠理论 400ms 放行。

只有 candidate-bound、registry-SHA-bound 的 publication authority 明示
`boundary_end_mode=exact_source_pin` 时，`required_given_end_ms` 才是官方 source 时间轴上的
**精确最终媒体 end**，不是普通下界，也不签发语义 PASS。source-full-window reviewer 仍须使
上述四命题全部成立；由于 fresh ASR cue timing 可相对官方 source cue 漂移，exact scope 把
`[pin-400ms, pin]` 内的完整语义句尾、加上唯一**包含 pin 且越过 pin 不超过 600ms** 的收尾
cue（`pin_crossing_closure_cue`，AGY 合并收尾时的有界计时差）列为可选 recommendation，
禁止选择 pin 之后才开始的 cue，forward recommendation 固定为 0。resolver 必须 snap 到
reviewer 所选 fresh cue（跨界收尾时即该 cue 本身），再由 source pin 精确补足/钳短
disposable tail；最终媒体 end 必须等于 pin。落在 semantic closure 之后的
fresh-ASR 漂移 cue 不取得交付字幕所有权，下一话题仍只作 source witness；
`talk-boundary-final-endpoint-binding.v1` 同时绑定所选 closure cue 与 pin 后的 exact final
interval（跨界收尾时按 cue 含 pin 的确定性containment重算，不信任 review 自述）。
registry/candidate/mode/ms 任一不匹配、可选集合为空、reviewer 选择 pin 之后才开始的
cue、required owner/structured payoff 越过 pin、grid/index 漂移或最终
媒体不等于 pin，均 fail closed；不得把 exact pin 降级为普通下界，也不得用它绕过四命题。

自动选片的 `semantic_end_ms` 只是召回尾锚，不等于人工下界。没有 `given_end_ms` 时，
source-full-window CPA 可在最多 15 秒的 `semantic_tail_trim_cap_ms` 内向前回剪，但只能选
**最晚一个**同时满足四命题的 cue，并须显式确认 `content_anchor_covered=true`；后续 cue
必须能证明是新话题、未回答的新问题或不完整尾巴。manual lower bound、structured payoff、
required owner 与 exact pin 均不得被这条窄门跨过。最终 scope 必须披露
`recommendation_backward_ms`，resolver 复算同一 SHA 后才可采用；final-delivery 层仍只审
实际成片最后 cue，不能在成片落地后凭文本结论偷偷再剪。

`content_boundary` 的恢复指纹必须覆盖完整的生产边界决策面：semantic reviewer、
request/scope 构造、owner/resolver、final-review contract 与 talk-lane 分类，而不只是顶层
producer/chunker。上述任一实现变化都必须在下一次 runner tick 唤醒既有 boundary failure；
无关 graph、crawler 或 reporting 变化不得制造重跑。

因用户只报告 1–2 个抽样问题而触发 same-BV **整片重跑**时，旧公开成片的 endpoint
不得被自动升级成 `exact_source_pin`。除非 Ivan 明确说已逐帧/逐句审过该候选的精确终点，
旧公开 endpoint 只能登记为 `published_recall_anchor`，由 source-full-window 语义评审在
前 15 秒至后 repair cap 的范围内重新寻找完整 payoff/闭环；它不是人工下界，也不是精确
终点。只报告字幕、标题或封面问题不构成精确终点授权。若旧 endpoint 已落入未完成尾句或
下一话题，CPA 必须在有界窗口内回剪到当前故事最后一个四命题均成立的 cue；若它早于更晚的
structured payoff，则必须保留 payoff 并重审，不能用旧公开时长反向截断故事。

source review、resolver 与有界 retry 必须共同消费并逐字段、逐 SHA 绑定同一份
`talk-boundary-search-scope.v1`，禁止各自从旧 candidate end 重新推导 cap。scope 按
`boundary_end_mode` 分成两种，不能混用：

- `semantic_lower_bound`：
  `semantic_search_origin_ms = max(semantic_target_ms, manual_lower_bound_ms,
  structured_payoff_ms)`；人工下界与已确认 payoff 属于语义搜索起点，可以把搜索原点向后移；
  没有人工下界时，`delivery_lower_bound_ms` 可在不跨越 structured payoff / required owner
  的前提下比 `semantic_search_origin_ms` 最多早 15 秒；存在人工下界时仍等于各权威下界最大值；
  required owner 只抬高交付/评审下界，不能移动搜索原点，也不能把 cap 滚动再加一次；
  `max_recommended_end_ms = semantic_search_origin_ms + repair_cap_ms`。reviewer 的推荐 end 必须
  同时不早于交付下界、不晚于该绝对 ceiling。唯一的有界例外是
  `silent_gap_closure_cue`：下限前**最后一个**收尾 cue，且它到下限之间不超过 400ms
  （=DELIVERY_TAIL_PAD_MS）并且该间隙内没有任何 cue 起点（纯静音，机器可证）；此时该
  收尾 cue 可被推荐为语义收束，交付下界本身不动，由 tail-pad 桥把媒体补到下界，因此不会
  丢任何已圈内容。间隙里有语音或超过 400ms 仍 fail closed；
- `published_recall_anchor`：registry 中的旧公开 source endpoint 只移动
  `semantic_search_origin_ms`，不进入 `delivery_lower_bound_ms`；
  `recommendation_backward_ms` 最多 15 秒，向后仍受 repair cap 约束。回剪只能选择最晚一个
  同时满足四命题、覆盖 content anchor 且以后 cue 已换题的闭环 cue；required owner 与
  structured payoff 仍是硬下界。registry/candidate/mode/ms 必须逐项绑定，普通生产 spec
  不得自行签发此模式；
- `exact_source_pin`：`semantic_search_origin_ms=delivery_lower_bound_ms=pin`，
  `minimum_recommended_end_ms=pin-400ms`、`max_recommended_end_ms=pin`、
  `max_forward_ms=0`。reviewer 在该 400ms 窗内选择完整 closure；当 fresh-ASR 收尾 cue
  合并跨过 pin 时（官方 cue 与 AGY 计时源不同），唯一包含 pin 的那个 cue 若越过 pin 不超过
  600ms（`pin_crossing_closure_cue`），可作为语义收束证人被推荐，其生效推荐 end 记为 pin
  本身，最终媒体仍精确截止在 pin。越界超过 600ms、pin 之后才开始的 cue、required
  owner/structured payoff 越过 pin 都阻断。两类放宽都必须在 review 与 resolver 两侧由同一
  确定性函数重算并在 `recommendation_relaxations` 里留证；
- retry 的 source full window 至少覆盖
  `max_recommended_end_ms + witness_reserve_ms`，再按 piece 映射回绝对 source 时间。reserve
  只供 reviewer 观察终点后的下一话题，不能扩大合法推荐 endpoint。

scope 缺失、SHA 漂移、resolver 重算不一致、source window 没覆盖 reserve，或 owner 下界已经
越过绝对 ceiling，都必须 fail closed；不得用旧的 `semantic_target + 60s` 或
`candidate end + 90s` 近似替代。

调用 source reviewer 前必须先落
`talk-boundary-source-context-coverage.v1`，逐字绑定 scope SHA、实际/必需 local source
context end 与 deficit。覆盖不足时**不得调用 LLM 自证**，而是返回 typed
`retry_scope=source_witness_reserve` /
`BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE`；runner 只允许一次 fresh source
重物化，endpoint cap 仍从当前 30 秒最多扩到 60 秒，并重新转录、重新生成 request/grid、
重新审查。首轮 piece post-context（PIECE_POST_MS=48s，人工下界超出 semantic end 的部分
逐候选加到 post pad 上）必须覆盖 origin+初始 cap(30s)+reserve(15s) 的常规需求，让常见
情况一次通过；短窗不省钱——它换来整窗重转录的 retry。未知 retry scope、scope 无效、源字节不足、已在 60 秒仍失败或第二次失败均终止，
不得空转、滚动 cap 或扩大到 120 秒。`recommended_end_cue_index=null` 应记
`BOUNDARY_RECOMMENDATION_MISSING`，不能冒充“给了一个越界推荐”。

source truth 的**修字作用域**与**边界所有权作用域**必须分开。所有命中 padded source
context 的 active truth 仍须正确应用。candidate recall/semantic start 与字幕 cue 起点允许
最多 500ms 的有界开场时间抖动，因此 immutable story scope 的 start 为
`max(padded_start, semantic_start_ms-500ms)`，end 仍为
`max(semantic_end_ms, given_end_ms)`；只有 source interval 完整落入这个 scope 的
`required=true` truth 才可取得 owner，并可把最终 start 拉回该完整 cue。普通
`story_content` truth 若完全位于更早 lead 或 post context，仍须修字和留证但不拥有本片
边界；开场跨界超过 500ms、任何尾部跨界或其他 scope straddle 都 fail closed，不能一半当
正文、一半当 context。经证据确认的下一话题文本可显式标为
`boundary_role=next_topic_witness`：它仍是
padded context 中必须正确落字的真值，可帮助证明换题，但不能把当前故事终点向后拖进下一条
SC，也不取得最终字幕 owner；与故事 scope 有任何重叠都 fail closed。

所有 talk 包——包括带人工 end 的恢复包——都必须通过**两层不同作用域的语义回执**，同时
通过确定性 cue/syntax 门与上述四命题。两层不能互相冒充，也不能把第一层的 cue ordinal
平移后当成最终成片证据。

1. `review_scope=source_full_window` 在 resolver 前运行。它消费当时完整的 source full-window
   cue grid，向推荐 endpoint 之后保留下一话题 cue，供 reviewer 直接证明
   `next_topic_separated`。request 与完整非空 cue grid 各自 hash-bound；resolver 只消费这份
   回执，并在 snap 后用 `talk-boundary-final-endpoint-binding.v1` 绑定 source 坐标中的推荐
   cue/ms、实际 `[final_start_ms, final_end_ms)`、closure 文本与 grid SHA。
2. resolver、裁切和 `_materialize_final_recut` 完成后，必须从**包内精确最终 SRT**重新解析
   delivery-local cue grid，再运行 `review_scope=final_delivery`。这一层必须按当前最终字幕
   重新判断 syntax/story，并把推荐 endpoint 绑定到 delivery 坐标（`final_start_ms=0`、
   `final_end_ms=成片内容时长`）的唯一最后 closure cue；它不能复用 source ordinal、request
   或 grid hash。

final-delivery SRT 已经裁掉终点后的 cue，因此它只能通过 PASS 的
`talk-boundary-source-separation-witness.v1` 继承“终点之后已进入下一话题”这一项证明。该
witness 必须绑定 source review 的规范 SHA、request SHA、source cue-grid SHA、推荐 end 及
实际 source final interval；它不替代对最终 SRT 的 syntax/story 重审。source review 没有真实
post-end witness、source interval 漂移、最终 reviewer 未选择 delivery 最后一条 cue，或任一
绑定不一致都拒发。

两层回执的 cue grid 与坐标系本来就不同，**不得要求两层 cue-grid SHA 相等**。应分别验证：
`boundary_audit.boundary_semantic_review` 是 `source_full_window`，而
`boundary_audit.final_delivery_boundary_semantic_review` 与 StoryContract 中的
`boundary_semantic_review` 是同一份 `final_delivery` 回执。source grid、snap 或 interval
变化会使 source 回执及其下游 witness 失效；materialize 后 SRT 的任何字节/cue 变化会使
final-delivery 回执与 exact-final 放行回执失效，均须从相应层重新评审，不能只重绑 hash。

若 exact-final 的 CPA 在 AGY 无有效听音（`UNCERTAIN`）时仅靠文字闭集提出修改，普通 cue
仍按 CPA 结果处理；但它不得把已由 PASS 的 final-delivery 回执及 source-separation witness
共同绑定的最后 closure cue 改成新的文本。此时 `terminal-closure-mutation-guard.v1` 以
`BOUNDARY_SEMANTIC_INVARIANT` 保留 CURRENT 并披露提案，避免文字推断把完整收束改成残句后
形成确定性重试死循环。AGY 有效听音后由 CPA 作出的声学裁决不受这条保护影响，改字后仍须
重新通过 final-delivery 语义闭环。

两层 reviewer 的 `evidence_cue_indexes` 都必须是各自 hash-bound request 中实际展示的
`cues` 的非空子集；引用未展示行报 `BOUNDARY_EVIDENCE_CUES_INVALID`。source reviewer 声明
`next_topic_separated=true` 时，至少一条 evidence cue 必须在推荐 endpoint **之后**，否则报
`BOUNDARY_NEXT_TOPIC_WITNESS_MISSING`；final-delivery reviewer 仅在上述 source witness
完整 PASS 且推荐 delivery 最后一条 cue 时可没有片内 post-end cue。候选的 hash-bound
`clip_context_prompt` 按 [40-subtitle-text.md](40-subtitle-text.md) 的完整 18,000 字预算原样
展示，不能先截成 12,000 字；超预算或可见 cue 窗超限均 fail closed。

每一层 semantic PASS 都只对它实际推荐的 endpoint 有效，并各自需要 PASS 的
`talk-boundary-final-endpoint-binding.v1`。若 snap、repair 或 materialize 改变该层 endpoint，
旧 PASS 不能沿用；只能在原有 cap 内有界重审/重试，仍不一致则以
`BOUNDARY_SEMANTIC_ENDPOINT_CUE_MISMATCH` /
`BOUNDARY_SEMANTIC_ENDPOINT_MS_MISMATCH` 阻断。正常首轮 cap 为 30 秒；只有有界重试才可把
同一 spec 的 cap 提升到 60 秒，不能另开无上限扩窗。实际生产入口必须把
`boundary_repair_extend_cap_ms` 只接到边界/终审调用；接线缺失或误接到相邻实体 authority
阶段属于架构失败，并由 production-entry seam test 固定。

## 冻结的 required owner

只有完整落在上述 immutable story scope 的 `required=true` 字幕源真值，以及具备相应 typed
ownership contract 且同样完整落在该 scope 的已应用 story/chat 修复，才可在定边界前冻结进
`frozen-boundary-owner-contract.v1`，逐项绑定
`owner_kind + owner_id + local_windows + owner_scope_sha256`。已审 baseline 是最终字幕文字
恢复/验活权威，不是内容选择权威，绝不能因旧稿起止区间而取得 boundary owner、强迫新切点
保留旧尾巴。`required:false` source truth 只是 best-effort，partial/proxy chat support、
context-only verdict 与被拒 proposal 都不得取得 ownership；
其中整句 `exact_read` 还必须由 whole-line gate 明示 `owner_eligible=true`，缺失/False 即
拒绝。sender/gift/coreference/entity 等窄槽修复不借用该整句字段，而按各自 slot-scoped typed
contract 冻结。最终 boundary audit 必须原样携带同一 owner 列表，且每个窗口完全落在最终
`[start,end)` 内。

首轮还必须冻结 `owner_eligibility_scope`、`owner_set_sha256`、
`deterministic_owner_set_sha256` 与整个 `contract_sha256`。有界 source-witness retry 只能
扩大观察用 post-context，必须把首轮 token 原样带入并重新计算。跨尝试绑定的是**确定性
部分**：`owner_eligibility_scope.scope_sha256` 与 committed-truth owner 子集
（`source_subtitle_truth`，其身份与窗口只由 ledger 区间和 immutable scope 决定）；任一漂移
即报 `BOUNDARY_RETRY_OWNER_SET_DRIFT`，不得让扩窗后出现的相邻候选 truth/SC 反向加入
本片故事。story-chat 类 owner（exact_read/sender/gift/coreference/entity）由每次尝试的
fresh ASR 匹配派生，毫秒几何甚至行成员在重转录后合法抖动，因此**按尝试各自冻结、各自
足额执行**，不参与跨尝试哈希；retry 回执必须披露
`asr_derived_owner_binding=per_attempt` 与两轮各自的 owner_set 哈希。空 owner 集
（全新场次、无 ledger 真值、无已应用 story-chat 决定）是合法冻结结果，交付层按无 owner
下界处理，不得崩溃。

旧候选边界若排除了 required owner，唯一合法结果是扩展边界、重新通过语义闭环和确定性门，
或以 `BOUNDARY_REQUIRED_OWNER_EXCLUDED` 阻断；不得把该 owner 在裁切后降级成
`NOT_REQUIRED`、`OUTSIDE_DELIVERY` 或普通 superseded 项。边界扩展仍受原语义 search origin
和当前 spec 的 repair cap 约束；owner 下界本身不是把故事无限延长的授权，cap 内找不到干净
闭环就拒发。最终 audit 还必须分别记录并通过
`required_boundary_owner_verification` 与 `delivery_coverage_verification`；前者证明每个
owner window 在成片内，后者证明最终媒体 end 没有落在人工/结构化/owner 共同下界之前。

VAD 只描述物理连续性：通过 source/语义闭环后，“切点后仍有人声”只记非语义告警，
不得触发一直延到下一静音/下一 cue 的盲扩展；句法硬尾（如“因为”“跟第……”）仍阻断。

选择 scorecard 与边界 reviewer 当前都来自 CPA gpt-5.6 模型族，必须共享同一
`independence_group`，只算一个相关语义证人；“分开调用”不等于两票。
