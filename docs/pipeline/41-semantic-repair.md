# 41 语义修复引擎（[40-subtitle-text.md](40-subtitle-text.md) 的核心子权威）

设计原则（Ivan 2026-07-19，源自 7/18 交付事故复盘 + 业界调研）：

## 分层架构

```
检测层（谁发现错）       证据层（谁提供候选）         裁决/强制层
─────────────────       ─────────────────────         ─────────────
词表/弹幕/ledger      →  文字 provenance + AGY 拼音 →  CPA 法官 → choke point
终审审片员 LLM       →  整片语境/指代/声学证据      →  CPA 法官 → mutation audit
漏听 recall 检查     →  候选与不可闻证据            →  CPA 法官 / fail closed
```

1. **检测≠裁决≠落地**，三层独立记账。7/18 事故的教训：检测层 6/6 全对，落地层全军覆没——事后审计必须能分清是哪层坏了（review-flags 的 `infra_unresolved` 字段就是这个用途）。
2. **CPA 是一般语义改字的最终裁决权，但保留期望值旁路**。AGY/ASR 是证人；普通文字证据
   只提候选。允许绕过 CPA 的三类是：Ivan operator truth、纯机械规范化，以及显式
   `expected-value canon`。后者只允许“未登记近音误听面→glossary/roster 登记规范词”且须过
   拼音门；current/proposed 都是登记词面时触发专名平等守卫，自动退出旁路交 CPA。
3. **裁决分层（Ivan 2026-07-19「不能绑死 Gemini 额度、也不能老用付费key」）**：
   - **T0 确定性旁路**：Ivan operator truth / 纯机械规范化 / expected-value canon——零模型。
     expected-value 每条保留 provenance、拼音门和 `registered_name_conflict=false` 收据。
   - **T0.5 同音候选**：glossary/roster 高先验且 current 未登记时可进 T0；两个登记词面冲突、
     其他 provenance 或越过拼音门的候选一律送 CPA。
   - **T1 见证近音**（旧 `witnessed_near_homophone_fix`）：`source_surface`、相似度和 typed
     orthography receipt 只提高候选可信度，不能直接改字；仍由 CPA 结合整片语境与指代选
     CURRENT/PROPOSED。raw glossary prose、同片 ASR recurrence、宽泛 context 只能召回。
   - **T3 声学证人**：AGY/声学层不得输出或决定汉字，只提供 `target_audible`、疑似拼音和候选
     发音兼容度。代码记录证据冲突但不得否决看过该证据后仍明确选择 `PROPOSED` 的 CPA；
     最终 mutation 只认 CPA 闭集裁决。量级 ~1/10。
     AGY 拼音是高可信辅助而非最高法官：明显只覆盖邻句/半句/错位窗口时，CPA 可按完整语境
     在闭集内定夺。CPA 若选 `NEITHER`，流水线用同一个候选盲音频几何让 CPA 提案层生成一个
     有界第三候选，再由独立闭集裁决确认；提案层无 mutation authority，第二次仍非
     `PROPOSED` 就保持 fail closed。
   - mixed CJK/Latin fidelity 门同样没有终审权：严格整句相似度命中可作为 verbatim 见证；
     未命中时，候选盲音频转写只作为 PROPOSED，与 CURRENT 组成闭集交 CPA。CPA 选择
     CURRENT 才能保留正常 code-switch，选择 PROPOSED 即由 mutation authority 继续校验后
     重写；拼音冲突只披露，不再构成第二票。CPA 不可用继续 BLOCK，禁止检测器或 AGY 自行选边。
     此处生产音频输入只交 AGY；CPA 只接收 CURRENT、AGY 候选盲文字转写与整片文字语境。
     AGY/音频抽取的 provider 故障必须分类为 `provider_transient / foreign_source_audio_witness`
     并由 runner 续跑，不能固化成 terminal `foreign_source_transcription`；只有 AGY 已返回有效
     文字证据而 CPA/文字权威仍无法闭合时才是内容门终局。
   - T2 备选未实施：免费 BCUT 对争议 span 重转写+拼音距离比对（「穷人声学见证」），T3 仍嫌贵时再上。
   - **删除专线**：`acoustic_delete` 仅提议删除一个有界疑似幻听 span，`acoustic_drop_cue`
     仅提议整条无声；AGY 的“不可闻”仍只是证据，只有 CPA `PROPOSED` 才能执行删除。严格
     postcondition 不成立、CPA 选 CURRENT 或 CPA 未完成就保留原文并阻断/披露。
4. **infra 失败不是裁决**：provider 额度耗尽导致的 UNCERTAIN 不许当终局，producer 以 `FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED` 拒绝带伤交付，runner 按 provider_transient 有界重试。correction discovery/routing 本身异常时，对 SRT 与 chat audit 必须原子回滚，保存 typed `AUDITOR_UNAVAILABLE` 原因、空 findings 与零 applied；后续 exact-final 空扫描不能洗白，只允许 `final_review_correction_discovery` 有界重试。若 CPA 返回非空 findings 但全部违反 finding schema，同一 discovery 最多追加一次 CPA schema-repair 复审；第二次 prompt 必须携带机器的逐行拒绝原因，要求每个怀疑要么补成有界 `proposed_full_cue`、要么撤回，仍无效才继续 fail closed，禁止把坏响应折叠成 CLEAN。`spoken_unit` 的小范围插入或删除只会产生完整 cue 闭集候选：须通过有界单段 diff、候选无关 AGY 见证、CPA 明确选中 `PROPOSED` 和 typed mutation receipt 才能落盘；整 cue 删除仍只走 `acoustic_drop_cue`。终审 transport 的内部模型链必须能在 caller 的外层 deadline 内完整耗尽：当前终审给三个获批模型各一次最长 180 秒请求，外层预算 600 秒；不得再配置成内部最坏 27 分钟、外层 5 分钟而必然被中途杀死的假 failover。
   exact-final 结转 finding 后，chat authority 可比先写的 review-flags 多
   `carryover_persisted_count`；该单一 additive receipt 不构成审计面矛盾，不能把原本
   `CORRECTION_DISCOVERY_INCOMPLETE` 的 provider transient 错分成 terminal contract。
   其他共享字段不一致或未知附加字段仍按 contract corruption fail closed。
5. **付费兜底**：同项失败≥3轮即可触发（额度类失败可同 run 连续补轮，`quota_exhausted_round`），每笔入帐。**Ivan 2026-07-19 明确否决冷却期类附加门**——控制付费用量靠 T1 分层缩减声学仲裁需求本身，不靠拖延付费。
6. **方言保真**：长沙话方言词（glossary「长沙话方言词保护」节）修复方向 = 方言原字 > 普通话意译 > 保留误听；通用中文纠错「归一到普通话」的默认方向在方言词上是反的。
7. **漏听 recall**：选片钩子/弹幕/SC 里的词表专名在字幕零出现 → 审片员漏听检查（prompt 规则7）→ 插入提案 → 声学仲裁（插入永远走 T3，不进 T1）。**已知盲区（2026-07-19 合并条实证）**：专名在片内它处出现过时零出现触发器不响，单句漏听无人怀疑（kmx 0:49 案，最终走 Ivan 审定 ledger 钉子）。改成逐句怀疑会假阳性爆炸；候选方向是「称呼/接话/突击等强语境句位 + 专名句位模板」的窄触发，进欠账。
8. **长程呼应属于片级语境**：检测器和审片员必须看到整片 cue 链，显式枚举
   `earlier_claim → later_callback / parody / correction`。后文对前文原句的调侃可用于定位“这里
   在复述哪句话”，但同源 ASR 仍不是独立文字权威；具体词面必须由音频、source truth、
   画面角色名或结构化弹幕/SC 见证。日期、联动对象、当场游戏/活动/公告只作为 scoped
   候选上下文，禁止变成无条件全局替换。
9. **别名按 mention 裁决**：`南町 / nightin`、`大N / 小N / 南町nightin` 等相似音节
   不能做整窗“统一词面”。每一次 mention 都绑定自己的 source interval、required text 与
   forbidden tokens；窗口内另一处写对，不能替当前 mention 通过。所有 mention 独立隔离后，
   精确 cue 并集才同时拥有 mutation 与 owner projection；预先写对也走同一投影。两个 mention
   合并进一条 cue、任一 mention 无法唯一归因或父窗口另有未审词面时，必须在 mutation 前阻断。
10. **结构化聊天的上下文命中不等于整句听见**：SC/弹幕的 `exact_span` 只是候选类型，不是
    authority。每一路 support 都必须写入 typed whole-line gate receipt，保存
    score/coverage/precision/extent/common 与 unsupported head/interior/tail。只有
    hash-bound raw-audio full-span、typed 近完整独立 transcript，或现行明示
    strong-thread-anchor 窄例外，才可令 applied row `owner_eligible=true` 并整句复制/冻结边界；
    missing/False、主 transcript fuzzy proxy、context-only verdict、低 coverage 或只命中几个
    词槽时，必须记 `PARTIAL_CHAT_EVIDENCE_CANNOT_AUTHORIZE_WHOLE_LINE_COPY` 并保留原句。
    已证实的昵称/实体槽仍可由 entity 或 source truth 单独修复，不能把未说出的 SC 余文一并补入。
    同一条结构化聊天重复出现同一个 canonical 时，重复本身不是“多实体歧义”：只有语义文本
    已保留相同 canonical、相同 mention 数量且逐槽规范时才可按双文本一致关闭；否则 mention
    数量/位置仍未决，必须以 `REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED` 阻断。单次实体选边不得
    推导多个槽位，更不得把重复实体证据升级成整条 SC 逐字复制。
11. **专名高先验与专名平等同时成立**：profile/glossary/roster 的规范词面可让未登记近音
    误听走 expected-value canon；profile 显式 `林墨 → 礼墨` 与 glossary 明列的三字以上
    单目标误听（如 `下斗里 → 沙豆李`）属于此类。两字常用词默认不进入无条件机械表，除非
    profile 对该精确对单独授权。若误听面以后也作为独立规范词面入表，registered-term guard
    会自动撤销旁路，交 CPA 判断。任何两个已登记专名/作品/
    梗词（如 kmx/乒乓球、恋青/恋死、梦限大/Mujica）都平等，禁止按频率互相覆盖。该 guard
    必须同时约束审片 finding 路由和所有 mutable stage 之后的最终机械重写；最终重写遇到
    双登记冲突时保留原字，写 `registered_name_conflict=true / CPA_REQUIRED` 收据。correction
    pass 必须在 protected-term 披露分支之前识别该收据，并把 CURRENT/PROPOSED 交声学见证和
    CPA 闭集裁决；“受保护”只能禁止机械覆盖，不能阻止 CPA 作出最终选择。
12. **晚期 source truth 只接受 operator 直改**：`subtitle_truth_ledger.v1.json` 的
    `source-subtitle-truth-governance.v2` 边界之后，每行必须有
    revision/state/evidence/authority/decision_authority；`VERIFIED_ACTIVE` 只允许
    `decision_authority=IVAN_OPERATOR_TRUTH`，否则拒绝加载。CPA/机器/结构化事件结论只能保持
    PROPOSED 候选，不得在 CPA 后面再覆盖字幕；旧错行必须 SUPERSEDED，不能原地改历史。
13. **operator truth 必须消解同槽旧阻塞**：chat/entity 层先发现
    `ENTITY_VERDICT_REQUIRED` 时不得在晚期 source truth 之前提前退出。正式 truth 投影落地且
    preview/formal ledger 哈希一致后，若投影内已逐槽保留结构化聊天 canonical，并达到原要求的
    mention 数量，则删除对应旧 requirement、记录 truth IDs 与计数收据，再重算 chat status；
    对 `REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED`，若聊天 matcher 从第二个 cue 才起窗，
    reconciliation 必须按同一非空 `source_event_id` 与同一 source SHA-256 汇集该结构化事件
    的 operator-truth 投影，不能只靠 cue 交集漏掉紧邻的第一个槽；事件 ID 或源 hash 任一不等
    仍只看 cue 交集并 fail closed。
    少一个 mention、无合法投影或其他未决槽仍 fail closed。该规则只承认 operator truth，不把
    结构化聊天本身升级成文字真值，也不绕过两个已登记专名之间的 CPA 裁决。

## 两次审查不可合并

- correction pass 输出 `final-review-audit.v1`，用途是发现问题、决定是否需要同音修复或声学
  仲裁。它在 source truth、reviewed baseline 和全部 finalizer 之前/之中运行，因此不是发布证明。
  所有 mutation 先 staged；任一 discovery/routing 异常必须恢复输入 SRT 与原 audit，并输出
  `status=AUDITOR_UNAVAILABLE`、`release_gate=BLOCK`、typed reason、`findings=[]`、
  `applied_count=0`。
- 全部 authority 和确定性 guard 落地后，必须对精确最终 SRT 再跑一次 discovery，输出
  `final-review-audit.v2`。该回执绑定最终 SRT SHA-256；只有 discovery 完整、显式合法的空
  findings、零未决项、release gate PASS、`subtitle-correction-mutation-audit.v1` PASS，
  且 boundary semantic review 与 `talk-boundary-final-endpoint-binding.v1` 均 PASS 才能交付。
  第二遍空 findings 不能洗白 correction pass 已经发生的无权 mutation。exact-final 新发现若
  已有完整 CPA `PROPOSED` mutation receipt，可在同一 producer run 按 cue/hash/request/timing
  全绑定合同落字并再次 exact-final；这不是 AGY 或检测器旁路，最终决定仍是 CPA。坏闭集
  `NEITHER` 先按上述第三候选闭环重建；最多五轮有 mutation 的同轮自愈加最后一次 clean scan，
  仍无法完整重算或净空才写 carryover 并阻断，禁止为消费一个已定案修复而无条件重跑 ASR、
  封面和整片生产。
  如果 finding 的初始 span 因改动幅度过大而没有形成 `proposed_full_cue`，但后续
  hash-bound 声学闭集请求已构造完整 `proposed_cue`、CPA 明确选中 `PROPOSED`
  且 mutation/timing 收据全部合法，同轮自愈和 carryover 必须使用该请求内的完整目标句。
  不得只保存 `suspect` 而丢失目标句，否则下轮只能重复披露、永远无法落字。
  correction pass 已按文本 hash 重放的 carryover 只有在新一轮 CPA 完整闭集已选
  `PROPOSED` 并落字，或明确选 `CURRENT` 并形成可披露决定时才算消费。
  若它仍只停在 `disclosure`，后续独立 exact-final 的随机空扫描不得洗白：发布合同
  必须报 `FINAL_REVIEW_CARRYOVER_UNCONSUMED`，sidecar 也必须保留供下轮继续。
  已有完整 exact-final CPA `PROPOSED` 闭集收据的 carryover 必须连同该收据持久化；
  下轮 exact gate 只在当前 cue 文本 SHA-256 唯一命中、起止毫秒与原请求完全相同
  时重放这份 CPA 决定，再必须跑一次 clean exact-final。文本或时间任一漂移就禁止重放，
  继续 fail closed；这不是 carryover 自己获得 mutation authority。若发布 validator 因同一
  remap 尚未消费而优先报 `FINAL_REVIEW_CARRYOVER_UNCONSUMED`，只允许由覆盖全部未消费
  base hash 的上述 exact replay 继续 self-heal；缺一个、hash/time 漂移或只修了无关 finding
  都仍须阻断。
- provider 异常、JSON 不可解析、根结构错误、`findings` 缺失/null/非列表、返回项全部无效，
  都是“没有完成发现”，不是“没有发现问题”；必须 fail closed。package auditor 还会用包内
  最终 SRT 重验 v2 回执，禁止复用 correction pass 或上一轮 SRT 的回执。

## 模块指针

| 职责 | 模块 |
|---|---|
| 词表/专名权威 | `term_authority.py`、`assets/lidousha/glossary.txt`（含方言节）、`entity_confusables.json` |
| 弹幕/SC 证据修复 | `chat_proposals.py`、`chat_repair.py`（阈值 score≥0.68/coverage≥0.60/precision≥0.52） |
| 见证人规则 | `subtitle_fidelity.py`（通用 mutation 的候选/fidelity 门；同音/近音正字法另须 `final_review_auditor.py` 的 typed textual authority receipt） |
| 终审审片员 | `final_review_auditor.py`（发现器；同音/近音候选、typed mutation receipt、声学仲裁路由与插入契约） |
| 最终字节放行 | `final_review_contract.py`（验 `final-review-audit.v2` 的精确 SRT hash、完整 discovery、零 finding、correction mutation audit 与 final boundary endpoint binding） |
| 声学证人/裁决 | `entity_audio_verifier.py`（候选盲黑帧片段，只回可闻性/拼音；quota 轮次+付费兜底）+ `read_aloud_llm_verifier.py` / `acoustic_witness_adjudication.py`（CPA 看完整闭集并最终选边） |
| 源真值 ledger | `source_subtitle_truth.py` + `subtitle_truth_ledger.v1.json`（Ivan 审定钉子，唯一不受 provider 故障影响的通道；已审定完整口播必须用 `replace_cue`，不能假设 ASR 仍保留待替换误词；整 cue 静音幻听用严格包含语义的 `drop_cue`，跨界即冲突停用；官方回放等替代源只能用 ledger 内显式 alias，且候选 piece 必须同时精确绑定替代源 SHA-256 与审定时间轴偏移，文件名相似不继承真值） |
| 付费兜底政策 | `gemini_backup_policy.py`（≥3轮 strikes + 日帽 + 入帐） |
| 梗词铁律 | `surface_canon.py`（直女→侄女等 hard canon） |

## 确定性怀疑编译器（2026-07-19 审片第二轮落地，`phonetic_scan.py`）

7/18 五件套二审的教训机制化——人工审片能抓 kmx 变体靠的是「拼音近似 + 弹幕零背书」，这两条都能编译：

- **词表拼音候选发现**（原欠账 #5）：逐 cue 滑窗 vs 注册实体 readings 的音节序列相似度；命中未注册面→临时混淆组送声学仲裁。校准夹具即 7/18 实案（皮毛熊/K头小/Q我熊→kmx、卖批/奶皮子→奶P、林更多→ありがとう）。已注册面/钦定词面（含被窗口包含）一律跳过——那是精确通道的辖区。cap=4/片、按分取前 N（噪声不许饿死真命中）。
- **短语级重复分歧编译器**（原欠账 #0，Ivan 说的「语义编译器」）：跨句 3-6 字 n-gram 重复 + 单字近音差 → 混淆组（抱/帮、零的人/零个人案）。语气词对儿（了/啦）不编、分歧字位落在钦定词面（侄女/和成天下）让位词表权威、长 gram 先行去重。
- **上线纪律（2026-07-20 修正）：两条 lane 目前为披露专用（phase 1）**——产出只写进 `post_semantic_entity_policy.*_disclosure_only` 审计字段供审片员/人工复核，不进声学仲裁、不产生改写。91_291 首跑实证：接入黑帧强制二选一后，仲裁顺从确认了「李小→立希」（滑窗骑在「小李小李」叠名上）和「粮之→祥子」两个伪候选并落盘改写——单次黑帧确认不满足保向铁律的证据门（本句音节+结构化弹幕/重复槽位支持）。扫描器已加叠名守卫（窗口与注册面出现位置重叠即跳过）。phase 2（见证充分的裁决通道）记欠账 #11。

## 答谢完整性 + 称呼串（2026-07-19，`chat_repair.py`/`surface_canon.py`）

- **断点吸附标点**（大叫案根因）：SC 对齐重排的相似度 DP 会把断点切进词中间；`_snap_split_to_punct` 把 ≤2 字尾巴挪过标点——完整的词在完整的 cue 里。
- **谢谢还原**（十麻乃/快乐猫猫案）：SC 字幕卡式行丢答谢动词时按同时轴 draft 见证还原前缀（T1 见证语义）；draft 没听到不动。
- **未答谢披露**（0:23 打码礼物案）：窗口内送礼人/SC 发送者无答谢锚点→披露名单（不改写），给审片员和音频仲裁当「刚刚没念过的送礼人」候选。
- **`_THANK_NAME` 收录「的钢镚」**（钢镚=2元SC 的她式称法）。
- **称呼串等价类**：{姐姐、妈妈、宝宝、老公、主人(、宝贝)} 式内、顿号包夹的单字近音 token 确定性补全（吗→妈妈）；句尾疑问形态不碰。

## 预算分配铁律（2026-07-20「脑海里根本没有冒出熊猫二字啊」案）

**一切有界预算必须先收集、后排序、再消费——绝不按到达序先到先得。**同一晚两处实证：拼音扫描器 cap 按扫描序截断时低分噪声饿死林更多（0.89）；念读近失仲裁帽（3）按证据序消费时，晚段真念读（score 0.624/precision 0.875，全场最高质量近失）被 t=818.9 的近似弹幕（0.594，还绑错了 cue）占掉末位名额，**静默出局、零审计痕迹**。修复：近失候选全量收集→同 cue 跨度去重（只留最高分）→按 (score, precision) 取前 3 送仲裁（`chat_proposals._discover_chat_proposals`）。诊断路径备忘：这类"该修没修"先查审计数组是否触帽（len==cap 即饥饿嫌疑），再复算 `_match_metrics` 对照发现门槛。

**弹幕时间模型（Ivan 2026-07-20 指正）**：事件时间戳＝发送时刻≠她看到的时刻——上屏渲染延迟随房间负载变化（脑海案实测 ~15s，她刚看到就立刻念了）。念读窗口的宽上界（90s）是这个物理事实的正确反映；排序永远按文本质量，不按时间贴近度（真没想到熊猫案的误绑抓在 +89.6s 窗口上沿、被音频正确否掉——窗口宽度的代价由质量排序+声学仲裁兜住，别用收窗口来治）。

## 钉子纪律（2026-07-19「只有kmx/十麻乃」拼贴病复盘）

Ivan 指正=该句整体替换的锚，不是插入片段：钉子文本必须是**改正后的完整口播**（详见 principles §十三新增两条）。7/19 修复了两枚带病钉子并为四条片补 36 枚 review-round-2 钉子（生成器对本地终稿 dry-run 全过）。标题 authority 不属于本步骤；只读 [60-title.md](60-title.md)。

## 已知结构性欠账（按性价比排序，做前先读调研）

0. ~~短语级重复分歧检测~~（2026-07-19 已落地，见上节）
1. ~~付费兜底不可达~~（2026-07-19 已修，`2da11e9`）
2. ~~infra-UNCERTAIN 带伤交付~~（同上已修）
3. ~~方言零覆盖~~（同上已修，词表持续扩充）
4. ~~专名零召回无修复通道~~（同上已修：source-backed 插入）
5. ~~专名匹配纯精确~~（2026-07-19 已落地拼音候选发现层，见上节；已知边界：比实体少一个音节的短回声面——零三/流沙型——不在滑窗射程，靠注册面精确匹配兜）
6. **本地可疑度粗筛缺失**：调研结论第一优先级（PPL/pycorrector 漏斗），把昂贵 LLM/音频调用集中到高可疑行。当前每片全量过审片员，成本可接受，暂缓。7/19 追加动机：3Dlive 乱码段（「三丢下我怎么办」）这类重度 garble 需要先被粗筛点名，才轮得到带话题提示的音频重听。
7. ~~**语义 QA 评审文本≠最终交付文本**~~（2026-07-23 已闭环）：finalizer 现在逐项验证
   source-truth owner 与 reviewed-baseline owner 在最终 clean/speaker SRT 的原时间窗真实存活，
   package audit 重新验收这些 attestation。选题 QA 仍不是文字权威，只提供 StoryContract 与
   callback 上下文。
8. **歌词正文绕过词表链**（LRC 是歌词权威，影响面小，记录在案）。
9. **幻听插入词的全量自动发现仍未完成**（2026-07-18 七星「为什么/偶像脸」案）：
   整句通顺但某词无声学证据。当前已能用 `acoustic_delete` / `acoustic_drop_cue` 对已发现
   项 fail closed 落地，并在最终 owner/SRT 门复验；尚欠的是覆盖所有 cue 的确定性发现器。
   候选方案仍是双源 ASR 差集 + 见证要求，插入词无双源支持则送声学仲裁。不得把“已有删除
   通道”误写成“所有幻听都会自动被发现”。
10. **称呼串跨 cue 续行**（kmx 2:18「…姐姐吗｜宝宝？主人？」形态）：句尾单字 + 下一 cue 以成员词开头的续行不在顿号规则射程，本轮由钉子修；类解需要跨 cue 枚举检测器（编成候选组送仲裁，不确定性改写）。
11. **动态候选的 phase 2 裁决通道**（2026-07-20 立希/祥子回归案）：披露专用的两条扫描 lane 要重新获得改写权，必须配「多证人门」——本句音节独立复核（非回声）、结构化弹幕/SC 佐证、或双源 ASR 一致中的至少两项；单次黑帧强制二选一永远不够。设计时同读 e46d36a 的 AGY 回声防御。
12. **expected_entity 为空的修复绕过未注册回退守卫**（91_291 cue43 发生→发现案）：`revert_unregistered_entity_repairs` 对 expected 为空的行直接放行——句级重复分歧仲裁产生的无实体改写不受该守卫约束。补法：空 expected 的文本改写同样要求注册面或见证，否则回退披露。

## 业界调研要点（2026-07-19，详见 commit 记录）

- RLLM-CF（prompt-only 四步分解：预检→定位→拟音→验证，验证不过保留原文）与 LIR-ASR（拼音一致性硬约束候选池，消融证明该约束是防过改写的关键）与本引擎架构同构，可直接借鉴 prompt 设计。
- ASR-EC 基准警示：中文裸 prompting 纠错无效甚至有害——印证「LLM 只报不改+声学仲裁」路线。
- 必剪/剪映黑盒无热词接口；软热词（词表+钩子+弹幕实体注入 prompt）是现实替代，已落地。
- 长期选项：自建 FunASR SeACo-Paraformer/Qwen3-ASR（方言优先+热词解码），残留错误率压不下去再评估。
