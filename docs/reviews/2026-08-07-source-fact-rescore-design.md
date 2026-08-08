# 事实修正后评分卡失效 → 终态拒绝：状态机缺陷审查与修复设计

日期：2026-08-07 · 触发案：`auto_220747_1271_1323`（狍哥案）· 状态：**设计稿，未实施**

## 1. 根因独立判断

复核代码后确认用户诊断成立，且缺陷比描述的多一环：

- `source_fact_review.review_and_repair_source_facts`（`src/autoslice/source_fact_review.py:541-562`）在
  hook 被修正且 LLM 评 `selection_scorecard_review != COMPATIBLE` 时直接返回
  `FAILED / REPAIR_SCORECARD_STALE / SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE`，并把回执里的
  `final_selection_hook/final_title` **重置回原文**（:557-558）——修正成果只存活在 `passes[-1]`。
- `producer_package_finalization.py:2378-2387` 把除 provider 故障外的一切 FAILED 包装成
  `SOURCE_FACT_REPAIR_EXHAUSTED`。本案只跑了一个 review pass、`provider_retries` 为空，"EXHAUSTED"
  字面失实——它其实是 marker 选择的兜底分支，不是重试耗尽的证词。
- `talk_lane.classify_talk_failure:1054-1059` 归类为 `("story_contract", "source_fact_repair",
  recoverable=False)`；`delivery_recovery.backfillable_talk_rejection:294-302` 把
  `story_contract + recoverable=False` 直接判 `candidate_rejected + story_contract_unresolved_backfilled`。
- **终态陷阱**：`requeue_recoverable_talks:1621-1640` 对 `candidate_rejected` 只有三个逃生通道
  （legacy_exact / subtitle_authority 四阶段 / provider_backfilled_foreign），`story_contract` 一个都不占。
  连 `scripts/revive_rejected_candidates.py` 都救不了它——该行仍持有"合法"旧 scorecard，
  `--restore-selection-scorecard-from-spec` 遇到已有有效卡会拒绝（:223-229）。
- 深层结构缺陷：**scorecard 与 hook 之间没有任何 hash 绑定**。`selection_scorecard_is_valid`
  只自验算术（tier/分数/理由码），StoryContract 按值嵌卡不入 hash（`story_contract.py:286-290`），
  contract rebuilder 闭包复用同一张卡（`producer_package_finalization.py:2294`）。
  "旧卡不能配新 hook"目前完全靠一次 LLM 判断，判完之后系统只有"死"一条路。

结论：拦截 stale scorecard 是对的（防偷用），把"卡旧了"等同"内容不可交付"是错的；
"EXHAUSTED" 的语义污染和 rejected 后无逃生通道把一个可修复状态焊死成终态。

## 2. 推荐状态机与数据结构

新增一个**非终态**处置 `selection_rescore_required`，位于 review 失败与拒绝之间：

```
source_fact_review: hook repaired + scorecard INCOMPATIBLE
   └─ 回执 decision=REPAIR_SCORECARD_STALE（保留，不改语义）
        └─ finalization 不再抛 EXHAUSTED，抛 SOURCE_FACT_REPAIRED_RESCORE_REQUIRED
             └─ classify_talk_failure → ("selection_rescore", "source_fact_repair", recoverable=True)
                  └─ pick.status = "failed" + rescore_pending receipt（不进 candidate_rejected）
                       └─ runner rescore 车道（有界）:
                            ├─ 重生成 scorecard 成功 → 修正 hook/title + 新卡 → 重排序
                            │     ├─ 仍入 Top-N → pending_talk 重排队（source_fact_repaired_requeued）
                            │     └─ 跌出 Top-N → talk_backlog（RESERVE，非拒绝）
                            ├─ provider 故障 → infrastructure_retry（不耗内容额度）
                            └─ 修正后故事仍无事实支持 / 无法生成合法卡 / 超上限
                                  → candidate_rejected + selection_rescore_failed（终态，带完整链）
```

### 数据结构

**`source-fact-rescore.v1` receipt**（挂在 pick 行，进 record 审计）：

```json
{
  "schema_version": "source-fact-rescore.v1",
  "candidate_id": "auto_220747_1271_1323",
  "review_receipt_sha256": "sha256:<整个 source_fact_review 回执>",
  "repaired_hook": "<passes[-1].final_selection_hook>",
  "repaired_hook_sha256": "sha256:…",
  "repaired_title": "<passes[-1].final_title 或 null>",
  "stale_scorecard_sha256": "sha256:<旧卡canonical bytes>",
  "stale_reason": "<selection_scorecard_review.reason>",
  "final_srt_sha256": "sha256:<交付字幕，与 review 输入同一>",
  "clip_context_sha256": "sha256:<同一 hash-bound sidecar>",
  "attempts": [
    {"rescore_fingerprint": "…", "outcome": "RESCORED|PROVIDER_UNAVAILABLE|UNSUPPORTED|INVALID_CARD",
     "new_scorecard_sha256": "…", "new_tier": 1, "new_effective_score": 74.5}
  ],
  "status": "PENDING|RESCORED_REQUEUED|RESCORED_BACKLOGGED|FAILED"
}
```

关键键设计：`rescore_fingerprint = sha256(failure_fingerprint + "\0" + repaired_hook_sha256)`——
探索确认现有 failure fingerprint 只吃 `kind\0stage\0消毒消息`，对本案是常量；按
`(fingerprint, repaired_hook)` 记账使**每个不同的修正 hook 恰好一次重评分**，而不是全局一次或无限次。
消费记录存 `rescore_consumed_fingerprints`（镜像 `final_review_carryover_consumed_fingerprints`，
`delivery_recovery.py:134-138` 同款），上限 `SOURCE_FACT_RESCORE_CAP = 2`/候选（一次修正一次追改；
第三个不同 hook 说明 review 本身在震荡，交终态）。

**新 failure_kind**：`selection_rescore`（recoverable=True）。不复用 `story_contract`——
旧 kind 的终态语义（backfill 判拒）保持不动，避免波及 `subtitle_entity_consistency` 与
`title_fact_consistency` 两个真终态邻居。

## 3. 精确到文件/函数的修改

1. **`src/autoslice/source_fact_review.py`**
   - `:541-562` 分支：回执新增 `rescore_candidate` 块（repaired hook/title、stale 卡 sha、
     scorecard_review 全文）。`final_selection_hook` 仍回置原文（保持"未授权不落盘"不变式），
     修正文案只经 receipt 传递。返回 status/decision/reason 不变（下游兼容）。
   - `validate_source_fact_review`（:662-752）：接受并复验新块的内 hash。
2. **`src/autoslice/producer_package_finalization.py`**
   - `:2378-2387` marker 选择：`reason_code == SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE` →
     `raise SystemExit("SOURCE_FACT_REPAIRED_RESCORE_REQUIRED: …")`；同时修 "EXHAUSTED" 语义污染——
     只有 `decision == REPAIR_EXHAUSTED`（真 5 pass 耗尽，:582-595）才配 EXHAUSTED 字样，
     `REPAIR_CYCLE` 等其余分支换中性 marker `SOURCE_FACT_REVIEW_UNRESOLVED`。
3. **`src/autoslice/talk_lane.py`**
   - `classify_talk_failure` 加分支（镜像 :1060-1065 的 provider 分支）：
     `SOURCE_FACT_REPAIRED_RESCORE_REQUIRED` → `("selection_rescore", "source_fact_repair", True)`。
   - pick 行落 `source_fact_rescore` receipt（从 staging 侧 `record["publish_staging"]["source_fact_review"]`
     的 passes 提取，:1765-1773 同点挂载）。
4. **`src/autoslice/delivery_recovery.py`**
   - `_talk_retry_decision`（:153-245）加 `rescore_retry` 路线：`failure_kind == "selection_rescore"`
     且 rescore_fingerprint 未消费 → 排队；provider 类 outcome 走既有
     `INFRASTRUCTURE_WAIT_FAILURE_KINDS` 通道（把 kind 留在 `selection_rescore`，
     由 outcome 字段分流，不伪装 provider_transient）。
   - `backfillable_talk_rejection`（:294-302）：`selection_rescore` 不进 story_contract 判拒集合。
   - requeue item（`_recovery_queue_item` :573-677）：携带 receipt；
     **此处替换 hook/title 为修正稿、清空 selection_scorecard 并标记 `rescore_pending=True`**。
5. **重评分执行位：`src/autoslice/semantic_candidate_selector.py` 新函数
   `rescore_candidate_scorecard(...)`**
   - 输入：修正 hook、hash-bound 最终字幕 cue 窗（`start_cue/end_cue` 从 pick 行**开始持久化**——
     探索确认现在只活在 selector 诊断 map 里，这是本设计要求补的持久化）、clip-context prompt。
   - 复用 `build_semantic_recall_prompt` 的 scorecard schema 段做单候选提卡 → 
     `normalize_selection_scorecard(..., start_cue=, end_cue=)`（:325-423，确定性算术/Tier 门原样复用）→
     `apply_reviewed_selection_calibration`（:491-535，锚定校准照走）→ `selection_scorecard_is_valid`。
   - 产出绑定：新卡 sha 写入 attempts；旧 source-fact receipt 因 `selection_scorecard_sha256`
     变化自动失效（:701/:707 现有复验），下一轮 produce 会对新卡+修正 hook 重新跑 source-fact review——
     这是**闭环自洽**：修正后的 hook 必须在新卡语境下再过一次事实门。
6. **`scripts/free_session_autoslice.py` + `candidate_selection.prioritize`（:802-937）**
   - rescored 候选以新卡进入正常 Phase-B 重排序；入 Top-N（`MAX_TALK_PICKS=5`）→ `pending_talk`，
     否则 → `talk_backlog`（既有 drain 机制 :815-817 天然支持后日回流）。
   - runner 侧新增行数走债务账本授权流程（本文件即出处）。
7. **投影四点**（探索 §6 的清单照改）：
   - `reporting.py:41-46` 处置元组加 `RESCORE_PENDING`；`:199-211` 把 `selection_rescore` 排除出
     `rejected_talk`；拒绝表新增终态 `selection_rescore_failed` 行的 `门=selection_rescore`。
   - `batch_terminal_state.py:131-137/:152-157`：rescore_pending 计入 `retry_wait` 侧
     （复用 `retry_epoch`），不算 failure、不算 review_ready。
   - `candidate_selection.py:130-153` exact 闭包处置枚举加 `RESCORE_PENDING`（见 §5）。
   - `delivery_recovery.py:40-47` `TALK_RECOVERY_FAILURE_STATUSES` 涵盖新状态。

## 4. 复用现有生成与排序

不新写评分逻辑：LLM 提卡 prompt 段、`normalize_selection_scorecard` 确定性算术、
Tier-1 admission、校准锚（`apply_reviewed_selection_calibration`）、`selection_rank_key`、
`prioritize` 全部原样复用。新代码只有"单候选提卡的取窗与装配"和状态机接线。

## 5. normal PRODUCTION vs exact recovery

- **PRODUCTION**：rescored 候选参与全场重排序，可挤掉/被挤掉（配额中性：Top-N 总量不变，
  被挤出者按既有规则进 backlog）。
- **exact recovery contract**（`prioritize:824-847` 合同分支）：**槽位不换人**。
  rescore_pending 行保持在合同槽位上，`exact_talk_contract_closure`（:90-195）处置枚举新增
  `RESCORE_PENDING` → 闭包 INCOMPLETE（诚实呈现，不伪闭环）；重评分完成后按新卡在**原槽位**
  继续边界/字幕/标题/封面/打包，绝不补入其他候选。跌出 Top-N 的判据在 exact 模式下不适用
  （合同即名单）；只有 rescore 终态失败才把槽位标 `FAILED_OR_NONCOMPLIANT`。
  禁止把行同时留在 picks 与 pending_talk（:129-131 DUPLICATE 检查——迁移必须是 move）。

## 6. 测试矩阵

| # | 场景 | 断言 |
|---|---|---|
| 1 | 仅标题修正、hook 未变、卡 COMPATIBLE | 原卡保留，产线直通（既有 `RESOLVED_CPA_SOURCE_FACT_REPAIR` 路径回归） |
| 2 | hook 修正 + INCOMPATIBLE | 抛 `SOURCE_FACT_REPAIRED_RESCORE_REQUIRED`，pick 带 PENDING receipt，不出现 candidate_rejected |
| 3 | 重评分成功仍 Top-N | 新卡 sha 入 attempts，pending_talk 含该候选，hook/title=修正稿，旧 source-fact receipt 失效重审 |
| 4 | 重评分成功跌出 Top-N | 入 talk_backlog=RESERVE，summary 不进拒绝表 |
| 5 | 修正后故事无证据支持（提卡 UNSUPPORTED/卡非法） | 终态 `selection_rescore_failed`，evidence 链完整 |
| 6 | 提卡 provider 5xx | outcome=PROVIDER_UNAVAILABLE，走 infrastructure 等待，rescore_fingerprint 不消费 |
| 7 | 同一 repaired_hook 第二次失败 / 第三个不同 hook | fingerprint 拒绝自旋，fail closed 保留 attempts 全链 |
| 8 | exact contract 下 | 闭包=INCOMPLETE + RESCORE_PENDING 处置；无候选补位；完成后原槽位闭环 |
| 9 | `auto_220747_1271_1323` 回归（用真实 receipt 固定件） | 走 2→3/4；"她被亲手背刺/最有安全感"按 §7 语义规则修正；"投奔求保护"按连续 cue 链判 SUPPORTED |
| 10 | 投影 | rescore_pending 批次状态=retry_wait 族，绝不 review_ready/candidate_rejected 误报 |

## 7. 语义层：合理概括 vs 无依据升级（source-fact judge 规则补丁）

探索确认 prompt（`_prompt:80-152`）已允许"非逐字自然概括"（:95-98），本案的判罚偏差在
justification 缺乏可审计结构。补丁（改 prompt + 回执 schema，不新增裁决权）：

- `changed_surfaces` 每行新增 `claim_decomposition`：`{actor, action, object, degree, outcome}`
  五槽逐槽标 `SUPPORTED_BY_CUES[cue…] | GENERALIZED_FROM[cue…] | UNSUPPORTED`。
- 判定规则（写进 prompt 的硬规则，机器可复核部分在 `_valid_changed_surface` 扩展）：
  - **允许 GENERALIZED**：连续/邻近 cue 链共同蕴含的间接言语行为（找狍哥→表忠心→跟你混→
    加入队伍→有安全感 ⇒ "投奔狍哥求庇护"），条件=每槽至少有一条 cue 证据、无槽为 UNSUPPORTED。
  - **禁止升级**（任一即 UNSUPPORTED，必须修正文案而非拒绝候选）：
    受事换人（刀由菜 ≠ 刀她本人）、因果虚构（求保护→被背刺的戏剧闭环）、程度升级
    （"非常有安全感"→"最有安全感"——degree 槽必须逐字或降级，不得升级）、提案→既成事实。
- 本案套用：概括"投奔求保护"= actor/action/object 三槽 GENERALIZED+SUPPORTED ✅；
  "她最终被狍哥亲手背刺"= outcome 槽受事错误 → 修正文案 ✅；"最有安全感"= degree 升级 → 降回"非常" ✅。
  修正后的 hook 配新卡重新走事实门（§3.5 闭环）。
- 字幕 authority 冲突（"你带着我，保护我" vs 窄窗听证"那我怎么保护"）：单 cue 的窄音频裁决
  只拥有该 cue 的字面，**不得反向抹除其他 cue 链的语义支持**——decomposition 按"全部相关 cue"
  取证，这条写进 prompt 的证据规则段（:104-108 旁）。

## 8. 风险与对策

- **循环**：fingerprint 按 (failure, repaired_hook) 一次性消费 + CAP=2 + review 自身 5-pass/振荡守卫不变。
- **重复候选**：exact 闭包 DUPLICATE 检查已有；迁移语义定义为 move（picks→pending 单向）。
- **配额超发**：rescored 候选不新增 Top-N 名额，只参与既有排序；backlog 回流走现有 drain。
- **旧证据污染**：新卡 sha 使旧 source-fact receipt 复验失败（现有 :701/:707 机制），
  StoryContract 由 rebuilder 以修正 hook + 新卡重建；本设计同时要求 rebuilder 闭包
  改为接收卡参数（消除 :2294 的隐式复用——这是根因级修复）。
- **需求异议（§8 要求明说）**：用户建议的状态名单里 `source_fact_repaired_rescore_pending` 与
  `selection_rescore_required` 语义重叠，本设计合并为后者 + receipt.status=PENDING，
  少一个状态少两处投影分支；"重新执行 Tier admission 和全场排序"在 exact 模式下按 §5 收窄为
  "原槽位继续"，否则会违反 exact contract 的不换人不变式——这不是放宽，是该需求自身
  （"exact 中不得静默换候选"）的一致化。

## 落地顺序建议

receipt/schema（source_fact_review）→ marker 与分类（finalization/talk_lane）→
recovery 车道与投影 → rescore 执行位 → prompt decomposition → 测试矩阵 9 件套 →
`auto_220747_1271_1323` 真实回归。全程不动 uniform 案例外的既有终态语义。

## 附录（2026-08-08）：主角维度反例数据点——auto_210739_1142_1436

Ivan 审后裁定：「没人抱团认南天为大哥」T1/86.5 高分成品**非李豆沙主角**（叙事主线在
他人），不可作为频道成品发布，仅作流水线优化材料（出版登记已 hold_pending_review）。

对本设计稿的含义：rescore 车道未来落地时，评分卡的语义维度必须包含**主角归属**
（selection metric v2 的「围绕本人」是硬维度——见 lidousha-slice-selection-metric），
且它是会被事实修正翻转的维度之一：hook 文字上"她"字当头不代表叙事主线在李豆沙。
本案是该维度静默失败的第一个 T1 级实例（sem 分聚合掩盖了主线归属），rescore 判
COMPATIBLE/INCOMPATIBLE 的 prompt 分解里应有单独的 protagonist 判项。仅记录，
不在本稿实施。
