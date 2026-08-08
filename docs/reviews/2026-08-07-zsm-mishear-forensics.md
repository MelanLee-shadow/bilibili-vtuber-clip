# 2026-08-07 鹅鸭杀场 auto_203735_555_680 误听法证复核

Ivan 对候选 `auto_203735_555_680` 的字幕文本逐字裁决（16/61 cue text_changed）。
真值来源：`ivan-speaker-truth-diff.v1`，`source_machine_sha256
89cc0251d80e1fef4f2427d4307fcfbbf6f8582fda9f2b34ee673ccff6fbdea7`。

## 逐 cue 根因

| cue | machine → truth | 根因分类 |
|---|---|---|
| 3 | 「…真善美了，美了」→「…真善美了」 | 回声尾字重复（前句「了」被同形字借读进本句尾，与 1863 boundary-borrow 姊妹病同族）|
| 10 | 「法医当一当二不如当三」→「法医」\|「当一当二不如当三」 | 说话人轮次欠切分：两人连续发言被 ASR 并入同一 cue，真值按 owner 拆句 |
| 11 | 「大家好」→「大家好我是小三」 | 过度纠正/cue 合并删字：`padded.fresh.srt` 上游 draft 独立生成过 cue「我是小三」（紧跟「大家好」），下游某纠正/短cue处理阶段把它删没了，非声学漏听 |
| 13 | 「我是警长」→「警长」 | 跨 cue 前缀渗入：「我是」实际属于 cue12 所有权，本 cue 重复携带了邻句已用过的「我是」，真值裁掉冗余前缀 |
| 17 | 「不也挺好的吗」→「不要再欺负我了好吗」 | 整句级误听（与真值无近音关系），非同音替换，判为声学/语境粗误听 |
| 24 | 「豆沙不知道什么是进雾」→「我根本不知道什么是进雾」 | 自称人称转换（豆沙→我）+ 漏听强调副词「根本」|
| 28 | 「天云海…把你都杀掉了」→「萱萱卡娅…把李豆沙刀了」 | **纯召回缺失**：review-flags.json 全文档无任何 cue_index=28 的 finding——候选生成阶段从未对这个 span 提名「萱萱卡娅」，不是裁决层覆盖或降权；萱萱卡娅已在 `psplive_roster.v1.md` 登记，但候选生成没有触达这一句。已在 `glossary.txt` 补充方向性误听面词条（见下）提高未来提名先验；未改任何裁决代码，因为没有证明是裁决层的机制性 bug |
| 30 | 「是，我就是复仇者…」→ 拆两位发言人 | 说话人轮次欠切分（同 cue10）|
| 31 | 「可惜可惜，随机一下」→ 拆两位发言人 | 说话人轮次欠切分（同 cue10）|
| 41 | 「我怎么知道李姐有这种智商」→ 拆两位发言人 | 说话人轮次欠切分（同 cue10）|
| 43 | 「也不是很了解女人的」→「李姐很了解女人的」 | 主语脱落 + 否定词插入误听（李姐→也不是），整句级语义误听 |
| 44 | 「你好，OK」→ 拆两位发言人 | 说话人轮次欠切分（同 cue10）|
| 51/52 | 「写真」→「信」 | 内容替换误听，「写真」「信」无调拼音不近音（xiězhēn vs xìn）；上下文本应延续 cue50「BW信」话题，怀疑与 cue59 同族的「语义合理性盖过声学证据」风险，但本次未在 review-flags.json 中追出对应裁决记录，未做机制断言，留作后续复核线索 |
| 58 | 「懂吗你」→「你懂吧」 | 句尾语序/助词误听，小型语序重排 |
| 59 | 「你知道我要殉情啊！殉情」→「你知道 我要偶遇！偶遇」 | **候选层覆盖声学证据**（见下，重点案）|

## cue59 重点法证：殉情 / 偶遇

**证据链（`/opt/bilive/autoslice/out/2026-08-07/auto_203735_555_680/` on `free`）：**

1. `padded_545600_728600.agy_refined.srt` cue62（fidelity guard 之后）：`你知道我要偶遇啊！偶遇` —— 独立听写/AGY 精炼阶段已经给出正确文本。
2. `padded_545600_728600.fidelity-audit.json`：`draft` 与 `kept` 均为「偶遇」，`attempted`「殉情」被标记 `REPLACE_UNWITNESSED` 并拒绝——这一关卡本身工作正常。
3. `auto_203735_555_680.review-flags.json`（cue_index 59 的 self-heal repair）：
   - `candidate_provenance`: `{"kind": "glossary", "surface": "殉情"}` —— 候选来自 game-glossary 注入（鹅鸭杀恋人机制词）。
   - `near_homophone_gate`: `{"tier": "core_span", "pinyin_similarity": 0.5}` —— **门槛虚高**：该相似度是在带复读的完整跨度「偶遇啊！偶遇」vs「殉情啊！殉情」上算出来的，共享的「啊！」结构性填充字把分数从核心两字词的真实值 0.308 抬到 0.5，超过本文件 0.45 的近音门槛。核心两字词（殉情 vs 偶遇）拼音相似度实测仅 **0.308**。
   - `judge.reason`：「拼音"dou ma ni zou a ai yo a"明显错位覆盖了前句"懂吗"及目标句开头、未完整听到差异段，而…死亡后果语境与绑定机制词"殉情"直接呼应，"偶遇"则语义不通」，`ranking` 给 PROPOSED p=0.96。
   - `policy_branch`: `CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT` —— 声学证人短且错位（`heard_pinyin: "dong ma ni"`，仅 3 音节，覆盖的是前句尾字「懂吗」而非目标差异段），触发了 `witness_conflict`；但 CPA judge 被允许在此分支纯靠语境覆盖声学冲突（这是既有 `adjudicate_with_witness` 架构的设计特性，其他合法用例如 `大N`/`礼墨` 依赖同一逃生舱且受 `orthography_ambiguous` 正向证据保护）。

**verdict：** 两次听写（AGY refine + fidelity guard 双重独立通道）都给出正确的「偶遇」；候选层把游戏机制词「殉情」作为高先验候选喂给 judge prompt（`text_evidence.candidate_provenance`），judge 承认证人错位/不完整，仍以语义合理性为由选中该候选（p=0.96），把两次正确听写覆盖成误听。这是**候选层信息喂给裁决层后单靠语义盖过声学冲突**的机制性缺口，不是声学听写本身的问题。

## 系统性修复（唯一落地项）

**位置**：`src/autoslice/final_review_auditor.py`
- 新增 `_glossary_session_candidate_undecidable()`（`_orthography_ambiguous` 定义之后，约行 341）。
- 接入两处 `adjudicate_with_witness()` 调用点：约行 2325（首次仲裁）与约行 2658（proposal-rebuild 重试后的二次仲裁）。

**规则（2026-08-07 首版，已被下方收窄修正取代其条件部分）**：当 `adjudicate_with_witness` 返回 `CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT`（即声学证人与候选冲突、judge 仍选 PROPOSED）且候选 `candidate_provenance.kind == "glossary"`（game/theme 词表注入）且**没有**任何正向文字证据（`orthography_ambiguous` 为 False——已排除 declared respell、strict homophone、ascii 发音键三类正向信号），则改判 `repaired=False`，`policy_branch="GLOSSARY_CANDIDATE_WITNESS_CONFLICT_ORTHOGRAPHY_NOT_DECIDABLE"`，保留原字幕。

已注册专名 respell（林墨→礼墨）、严格同音对、ascii 发音键等价（大大恩/大N）三类合法覆盖场景全部因 `orthography_ambiguous=True` 而豁免，不受影响（回归测试全绿覆盖此三类）。

**范围收窄理由**：没有对 `_near_homophone_gate` 本身做通用「差异段隔离」重算——虽然该函数在本案里因复读填充字把 0.308 抬到 0.5，但同一算法对文档里已证实合法的近音案例（查烟查/恰烟恰 0.636→隔离后 0.428，低于门槛）会误伤，属于跨很多其他生产用例的高风险改动。改为在**候选来源为 glossary 且缺乏独立正向文字证据**这一更窄、更可验证的条件上拦截，只影响本类失败模式已证实存在的分支。

**回归测试**：`tests/test_final_review_auditor.py::test_glossary_candidate_cannot_win_bare_witness_conflict_on_semantics_alone`——用生产实况的 suspect/suggestion/witness/judge 复刻 cue59；已用负向金丝雀验证（临时回退 `final_review_auditor.py` 后该测试真实失败并复现「殉情」顶替「偶遇」），排除测试假绿。

### 2026-08-07 收窄修正（Ivan 回归纠正）

Ivan 指出首版门槛把 `candidate_provenance.kind == "glossary"` 本身当唯一拦截条件，范围过宽：历史上大量案例正是靠 glossary/roster 登记的误听方向（kmx 系「停放熊」等误听面、林墨→礼墨、大恩→大N 等）在声学证人本身破碎/错位（短、错位、覆盖前句）时正确顶替声学证据——这是 ASR 局限下刻意设计的既有行为，不能被 kind==glossary 连坐拦掉。核对发现首版的漏洞是真实的：kmx 类误听面方向（如「停放熊」）只登记在 `term_authority.expected_value_respell_pairs()`（走 `assets/lidousha/glossary.txt` 的 expected-value-canon 通道），不在 `respell_pairs()`（`_declared_respell_edit`/`orthography_ambiguous` 只查后者）——所以首版会把这类历史合法覆盖误判为「纯语义盖过声学」而拦截。

**收窄后规则**：拦截需要**同时**满足三个「缺席」条件才成立——(a) `orthography_ambiguous` 为 False（同音/ascii 发音键/已声明 respell 均缺席）；(b) `registered_direction` 为 False（`(suspect, replacement)` 有向对既不在 `respell_pairs()` 也不在 `expected_value_respell_pairs()` 中——kmx 类误听面方向由此纳入覆盖，方向单向，反向对不豁免，遵循「授权保向铁律」）；(c) `structured_text_support` 为 False（候选替换词面或 `candidate_provenance.surface` 都未被任何 sha256 绑定的弹幕/SC 命中，且 proposed cue 不在已登记 exact-cue canon 中）。三者任一为真即放行，只有三者皆缺、纯靠 judge 语义盖过声学冲突时才拦截。cue59 的「殉情」在三条件上均为缺席（未登记为「偶遇」的误听方向，也没有独立结构化文字支持），仍然照旧拦截，负向金丝雀保持全绿。

**新增正向金丝雀**（`tests/test_final_review_auditor.py`）：
- `test_glossary_candidate_with_registered_misheard_direction_wins_witness_conflict`——用 `停放熊 -> kmx`（仅登记在 `expected_value_respell_pairs()`）复现同一破碎证人/judge PROPOSED 组合，证明收窄后放行；回退到收窄前代码会转为失败（错误拦截），证伪测试假绿。
- `test_glossary_candidate_with_bound_structured_chat_support_wins_witness_conflict`——与 cue59 负向金丝雀完全相同的事实模式（同一「殉情/偶遇」候选、同一破碎证人），唯一变量是附加一条 sha256 绑定的弹幕独立佐证「殉情」，证明收窄后 `structured_text_support` 放行；回退到收窄前代码同样转为失败，证明不是巧合通过。

**任务前提核查（重要）**：交办本次收窄时曾假设 84e3603 已经修了 `_near_homophone_gate` 的「差异段相似度」bug（复读填充字把 0.308 抬到 0.5）。核实结果：**该 bug 未修**，且是 84e3603 自己在上面「范围收窄理由」一段中明确记录的、刻意不做的改动（会误伤查烟查/恰烟恰等已证实合法案例）。本次收窄未新增该重算，维持 84e3603 的原判断；cue59 仍然因为 (a)(b)(c) 三条件皆缺席而被拦截，不依赖这条未落地的相似度修复。

## glossary.txt 词条

`assets/lidousha/glossary.txt` PSPLive 小节新增一条方向性误听面（不做全局替换规则）：

> 萱萱卡娅…ASR 误听面方向：**天云海→萱萱卡娅**（来源：Ivan 2026-08-07 auto_203735_555_680 裁定）。方向单向，逐处仍须音频/语境仲裁成立才改写；「天云海」当前未登记，属于 current-unregistered→registered 场景。

未新增偶遇/殉情词条（Ivan 明确指示：偶遇不是游戏术语，不入 game glossary；cue59 已由上面的裁决层守卫机制性覆盖）。未新增欺负/写真/信等词条（均为普通近音/内容误听，无三字以上专名可锚定，按「授权保向铁律」只在本文档记方向，不建全局替换规则）。

## 真值落盘（B 部分）

候选级已审字幕基线：
- `assets/lidousha/reviewed_subtitle_baselines/auto_203735_555_680.reviewed.srt`（61 cue，truth_text，machine 时间轴，无说话人标签，无 A/B 标记；sha256 `49e63c6c23026226b64227d7b1bd503df0f520770b86abb3c941fa0498b31c0e`）
- `assets/lidousha/reviewed_subtitle_baselines/auto_203735_555_680.subtitle-baseline.v1.json`（`subtitle-redelivery-baseline.v2`，`exact_interval_replay=true`）
  - `source_recording_basename`: `22966160_20260807-20-37-35.mp4`
  - `source_sha256`: `100a7cd3a1949c12a73eb764e06f2555cb3fcbf4db4fc8912ea044271662d6b1`
  - `absolute_source_start_ms`: `555430`，`absolute_source_end_ms`: `681000`（取自 `auto_203735_555_680.recut.provenance.json` 的 `final_recut.absolute_source_{start,end}_ms`，即最终交付边界，而非 padded piece 的 545600–728600 原始窗口）

已用 `reviewed_subtitle_baseline_registry.load_candidate_reviewed_subtitle_baseline()` 与 `subtitle_validation.validate_srt_file()` 实测加载校验通过（61 cue，PASS，无 error/warning）。
