# 终审 keep-current 白名单落后引擎 14 天 —— 8/9 谈话切 4/4 全灭的真判据

日期：2026-08-10
工作树：`/Users/ivan/Project/vtuber-slice-wt/carryover`（分支 `tmp-carryover`，base `e602d33`）
纪律：未 deploy、未写 free 的 state/out/repo（free 只读取证）；终审层一个字节没改。

---

## 0. 先纠一条前提：两拨 finding 被混成了一拨

交给我的简报把两个**结构上完全不同**的群体当成了一个：

| 群体 | 形态 | 归属 | 现状 |
|---|---|---|---|
| **A. 判官裁"该改"** | `repaired=True` / `..._APPLY_PROPOSED` / `mutation.status=PASS` | **carryover 侧车**（下轮 correction pass 重出字节） | 通道存在且在跑 |
| **B. 判官裁"保留原文"** | `repaired=False` / `..._KEEP_CURRENT` / `mutation.status=NOT_APPLIED` | **披露出口** `unresolved_findings_disclosed` | 分支名没登记 → 永远回不到 resolved |

简报里那条逐字样本（`我觉得她还行吧` → `我觉得TA还行吧`，`repaired=true`）属于 **A**，
而 8/9 那 4 个死掉的谈话切，**5 条 finding 全部属于 B**——没有一条是 `repaired=true`。
两者的修法完全不同，混在一起会得出"要么不修、要么选 D 把错字发出去"的假二选一。

另：简报里的选项表（B 解锁 1/5）是在**已经不存在的快照**上量的。free 上的 runner 今天
一直在重跑，每个回执都被改写过。本报告所有数字都基于下面这批钉死的快照。

### 钉死的快照（下载于 2026-08-10T10:20–10:21Z）

| 文件 | sha256(前16) | bytes |
|---|---|---|
| `auto_190617_473_766.review-flags.json` | `8d72e557b2aaac2a` | 257086 |
| `auto_193611_1250_1450.review-flags.json` | `de7cea1acc44e6c6` | 206630 |
| `auto_193611_1612_1693.review-flags.json` | `b0ede7b89553d306` | 227654 |
| `auto_214238_835_960.review-flags.json` | `77fa2bda414999da` | 328365 |
| `auto_213743_1635_1717.review-flags.json` | `8554bece0265f7e4` | 210197 |
| `auto_213743_1635_1717.final-review-carryover.json` | `be6103a5ad1bf453` | 36117 |

生产路径：`free:/opt/bilive/autoslice/{out,state}`（不是 `~/vtuber-slice`）。
部署树 `/opt/bilive/autoslice/repo` 无 `.git`，但其 `final_review_contract.py`
的白名单与本工作树 HEAD 逐字一致——即本修复对准的就是生产在跑的那份代码。

---

## 1. 任务 1：carryover 回灌路径 —— **存在，已接通，在跑**

简报设的"若路径缺失就先报告不要实现"的升级条件**没有触发**。

### 全链路（都在 src，无缺口）

| 步 | 位置 | 动作 |
|---|---|---|
| 写入 | `src/autoslice/producer_package_finalization.py:1735` | 终审合同报错后、抛 `SystemExit` 前，`persist_final_review_carryover(carryover_file, audit)` 落盘 |
| 筛选 | `src/autoslice/final_review_carryover.py:179-216` | 只收 `exact_release_adjudication.repaired is True` 的 finding |
| 清账 | `producer_package_finalization.py:1756` | 干净一轮后再调一次，清掉已消费的行 |
| 同轮重放 | `producer_package_finalization.py:1119-1160` `_replayable_exact_final_carryover_findings` | 按 `base_text_sha256` + 时间轴精确匹配，重放上轮 CPA 决定 |
| 下轮消费 | `src/autoslice/producer_text_pipeline.py:768` | `load_final_review_carryover()` → `priority_rows` → `audit_final_subtitles(extra_raw_findings=…)`，走**同一条** route→adjudicate→apply 链，零特权 |
| 未消费守卫 | `final_review_contract.py:222` | `FINAL_REVIEW_CARRYOVER_UNCONSUMED` 反向兜底 |

### 实测证据

- `out/2026-08-07/auto_213743_1635_1717.final-review-carryover.json` **有 3 行**，
  全部 `repaired=True`（`CPA_SEMANTIC_ORTHOGRAPHY_TIEBREAK_APPLY_PROPOSED` ×2、
  `WITNESS_JUDGE_APPLY_PROPOSED` ×1）。B 的裁定确实被捞住了。
- `out/2026-08-08/auto_230125_714_859` 的 state 记录：
  `failure_stage=final_review_carryover`，`carryover.declared_count=3 / observed_count=3 / retry_ready=true`。

### `final_review_carryover` 那类失败是什么

`src/autoslice/talk_lane.py:940-951`：它**不是**"回灌跑了但失败"，而是
"回灌**成功落盘且自洽**（声明数 == 观察数 == 期望集合）→ 标 `recoverable=True`，
下一轮重试"。8/7 出现 3 次 = 这条路正常工作了 3 次。
对照组是 `final_review_findings`（`recoverable=False`）—— 没有任何可结转的东西。

### 那 5 条 8/9 的 BLOCK finding 为什么没进 carryover

**因为它们不该进。** 5 条全是 `repaired=False`，判官的结论是"保留原文"，
没有任何字节需要上游重出。carryover 的筛选条件（只收 `repaired=True`）是对的。
真正卡住它们的是下面第 2 节。

### 附带发现（**不是**今晚的拦点，记入剩余风险）

`persist_final_review_carryover` 只挂在**优雅的合同报错出口**上。硬退出——
`auto_200615_1320_1467` 的 5400s 超时、"producer exited without diagnostic"（8/8 出现 4 次）、
dts 崩溃——都会**跳过**落盘，那一轮 B 已经付费拿到的声学裁定直接丢失。
实测吻合：8/8 的 `auto_210131_1576_1802` / `auto_213135_806_1068` / `auto_230125_960_1072`
的 BLOCK 回执里各躺着数条 `repaired=True`，却**都没有** carryover 侧车文件，
且 state 里没有 `failure_evidence`（= 走的硬退出）。
修法方向是把落盘挪到 `finally` / 信号处理，但那要动交付主干，不在本次范围。

---

## 2. 任务 2：白名单落后引擎 14 天（本次修复）

### 判据链

`final_review_contract.py:456` `decided_keep_current_adjudication` 要求：

```
status == "OBSERVED"
and policy_branch in _DECIDED_KEEP_CURRENT_BRANCHES
and repaired is False
and timing_immutable
and mutation_authority.status == "NOT_APPLIED"
```

前 3 名单只有 3 个名字。git 考据（`git log -S`）给出了完整年表：

| commit | 日期 | 事件 |
|---|---|---|
| `ff68178` | 07-26 | Phase 1 声学证人架构落地，产出 `TARGET_INAUDIBLE_KEEP_CURRENT` / `JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT` |
| `0a97deb` | 07-27 | 写下白名单三名字 —— **当时三个都是活的产出点** |
| `d71e856` | 07-28 | "make CPA the final acoustic judge"：**删掉**了 `TARGET_INAUDIBLE_KEEP_CURRENT` 和 `JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT` 两个产出点。白名单没跟 → 3 个名字死了 2 个 |
| `5a43ea3` | 08-08 | Ivan 8/8 卡1 裁定"贴音优先、证据兜底"：证据门的继任出口落为 typed 分支 `WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT`。白名单又没跟 → 新名字从未登记 |

核验：`grep -rl` 在 `src/` 里，`JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT` 与
`TARGET_INAUDIBLE_KEEP_CURRENT` **零产出点**（只活在合同表和测试里）；
真正在跑的 decided-keep 只剩 `JUDGE_KEEPS_CURRENT` 一个。

所以这不是"放宽标准"，是**白名单静默失配了 14 天**，而且新加的名字正是白名单里
那条已死名字的直系继任者。

### 补入的分支：`WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT`（仅此一个）

语义逐条对照收录门槛（`src/autoslice/acoustic_witness_adjudication.py:1044-1078`）：

1. **`repaired is False`** —— 该分支 `return False, WITNESS_CONFLICT_UNSUPPORTED_PROPOSED, audit`，
   返回值第一位就是 `repaired`。字节零改动。✅
2. **`mutation.status == NOT_APPLIED`** —— 没有走 mutation 授权路径；
   5 份真实回执逐条核对，全部 `NOT_APPLIED`（`basis: null`）。✅
3. **"机器已决定"而非"机器没能决定"** —— judge 已经出结论（选了 PROPOSED），
   证人链也跑完了（有 `heard_pinyin`、有相似度打分）；是**代码级证据门**
   （三逃生口 `orthography_ambiguous` / `registered_direction` /
   `structured_text_support` 皆缺）主动否决了 PROPOSED 并保留 CURRENT。
   门本身就是决定——与白名单里 `JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT`
   的注释「judge 选了 PROPOSED 但代码级拼音门否决——门本身就是决定」逐字同构。✅

实现上不写字符串字面量，直接 `from src.autoslice.acoustic_witness_adjudication import
WITNESS_CONFLICT_UNSUPPORTED_PROPOSED`——**引擎再改名时 import 就断，不会再静默失配**。
（无循环依赖：`final_review_contract` 本来就 import 该模块，反向没有。）

### 明确**不**补入的（逐条理由）

| 分支 | 出现次数 | 不收的理由 |
|---|---|---|
| `JUDGE_REJECTS_CLOSED_SET` | 1 | 机械上确实 `NOT_APPLIED`，但判官的结论是"CURRENT 和 PROPOSED **都不对**"。这不是"保留原文"的裁定，把它当已决 keep 发出去是内容判断，Ivan 没拍过 |
| `JUDGE_UNCERTAIN_KEEP_CURRENT` | 1 | `status=UNCERTAIN`，判据要求 `OBSERVED`。属简报的**选项 C**，另案 |
| `HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY` | 3 | 同上，`status=UNCERTAIN`。**见下方张力条** |
| `WITNESS_NEVER_ATTEMPTED_KEEP_CURRENT_DISCLOSED` | 0 | 8/10 F21 新开；作者注明"默认关死待 Ivan 复裁"。零 BLOCK 出现，无实测收益 |
| `WITNESS_UNAVAILABLE_*` / `JUDGE_UNAVAILABLE_*` / `STALE_BASE_*` / `INVALID_OR_UNCERTAIN_*` | 0 | 基础设施未走完 = fail-closed，docstring 已划对这条线 |
| `CPA_*_APPLY_PROPOSED` 系 | 17 | `repaired=True`，属群体 A，归 carryover |

两个已死名字（`JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT` /
`TARGET_INAUDIBLE_KEEP_CURRENT`）**保留不删**：老回执可能重放它们，删除是零收益的 churn。
已在注释里标注"无产出点，仅为历史回执兼容"。

### ⚠️ 张力条：`HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY` 端到端是死的

这个分支的名字里就写着 `DISCLOSURE_ONLY`，`exact_final_witness_authority.py:23`
的常量叫 `DOWNGRADE_BRANCH`，整条代码路径是**为了"降级成只披露、随包发出去"而建的**。
但它带的是 `status=UNCERTAIN`，而 `decided_keep_current_adjudication` 要求 `OBSERVED`
——所以它**从来没有真正披露过任何一条**，永远落在 blocker 侧。

而 `producer_text_pipeline.py:1116` 分流点的注释（引的是同一条 7/26 裁定）写的是：
> 「judge 走完仍 **UNCERTAIN** 且策略分支为 KEEP_CURRENT 的 finding 是已完成的机器决定
>   ——按现文本交付并披露，发后可修；结构性失败照旧 fail-closed。」

**分流点注释说 UNCERTAIN 可披露，合同判据要求 OBSERVED。两处互相矛盾。**
这就是简报里的选项 C。它单独卡住 `auto_214238_835_960`（8/9 唯一一个本次没解开的）。
我**没有**动它——需要 Ivan 裁定"judge 走完但仍 UNCERTAIN"算不算已完成的机器决定。

### 顺带修的幽灵测试名

`tests/test_final_review_contract.py` 的 infra 负向表里有两个 `src/` 中**零产出点**的名字：
`ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED_KEEP_CURRENT`、`PINYIN_BACKEND_UNAVAILABLE_KEEP_CURRENT`
——断言的是不存在的形态（白名单脱节的同源症状）。已换成引擎真吐的
`JUDGE_REJECTS_CLOSED_SET`，其余名字逐个核过都有产出点。

### 第四个消费方核查

`src/autoslice/deferred_same_cue_resolution.py:149` 也用 `decided_keep_current_adjudication`。
加名字后，该分支的 `final_disposition` 从 `UNRESOLVED` 变成 `KEEP_CURRENT`——语义一致
（已决的 keep 确实是终态）。`final_disposition` 在 src 里只被 `== "SUPERSEDED"` 消费过
（`:326`），不影响任何字节路径。无其他 keyed-off 白名单的地方。

---

## 3. 任务 3：伪裁定核查 —— **署名为真，但被引申的范围不是 Ivan 划的**

前一位 worker 用 `search_session_transcripts` 查不到，是**工具没够到 raw JSONL**。
改用直接 grep `~/.claude/projects/**/*.jsonl` 并过滤 `type=user` + 排除 `tool_result`，
一次命中（5719 个 session 文件全扫）。

**出处**：`~/.claude/projects/-Users-ivan-Project-vtuber-slice/b533569f-161f-4656-bec9-a512bd639042.jsonl:1223`
**时间**：`2026-07-26T17:45:10.775Z`（= 本地 7/27，与注释署名的日期一致）
**元数据**：`type=user`、`userType=external`、`isSidechain=false`、`cwd=/Users/ivan/Project/vtuber-slice`
—— 真人 turn，非 sidechain、非 agent 注入。

**逐字**：
> 没有任何纪律要求必须5个全complete才能动BV，修复时哪个好了就可以改哪个。另外我还是要强调，流水线最终是无人值守的，不能因为没有人工参与就fail，必须得想个办法解决。目前是开发阶段我可以给真值，但是生产阶段是没有人工真值的，最多就是发出去了我检查有问题了再修，而不是一直不发。你现在让我裁定什么，拉到本地了吗？

**结论**：
- 署名**真**。不是伪裁定。
- 但裁定的内容是「**无人值守不许因为没有人工真值就永久阻断，已决的东西发出去、有问题再修**」，
  Ivan **没有枚举任何分支名**。"白名单只能有这 3 个名字"是实现者当时按引擎快照写的，
  不是 Ivan 划的线。
- 因此补入引擎改名后的继任分支，**方向与该裁定一致**（少阻断、按现文本发出去并披露），
  是在**执行**这条裁定而不是绕过它。注释已改成引用可核验的原话与出处，署名保留。

**方法论留档**：`search_session_transcripts` 的负结果**不足以**判定伪裁定；
raw JSONL grep（过滤 `type=user` + 排除 `tool_result` + 打印命中上下文）才是可靠的取证手段。
脚本留在 scratchpad，下次伪裁定核查照此办理。

---

## 4. 实测解锁（离线复算，非推演）

方法：下载 free 真实 BLOCK 回执 → `sys.path` 挂本工作树 → import **本仓真函数**
`is_keep_current_disclosed` → 对存档 `findings` 逐条重跑分流。

**声明层级注意**：存档回执里 `status=FLAGGED` / `release_gate=BLOCK` 是烤死的，
不能把它直接喂 `validate_final_review_release` 然后拿报错当结论。
诚实表述是：**所有 N 条 finding 都重分类为 disclosed ⇒ 当初产出 FLAGGED 的那个分流
现在产出 CLEAN ⇒ `FINAL_REVIEW_UNRESOLVED_FINDINGS` 不再抛出**。
真实重跑的 discovery 是非确定性的，可能翻出新 finding——这不是"保证发得出去"。

### 8/9 那 4 个（简报点名的决定性集合）

| 候选 | 存档 findings | 修复后 disclosed | 仍拦 | 结果 |
|---|---|---|---|---|
| `auto_190617_473_766` | 1 | 1 | 0 | ✅ 不再抛 UNRESOLVED_FINDINGS |
| `auto_193611_1250_1450` | 2 | 2 | 0 | ✅ |
| `auto_193611_1612_1693` | 1 | 1 | 0 | ✅ |
| `auto_214238_835_960` | 1 | 0 | 1 | ❌ 仍拦（`HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY`，`UNCERTAIN` → 选项 C） |

**5 条 BLOCK finding → 解锁 4 条；4 个候选 → 解开 3 个。**
（简报旧表的 "B 解锁 1/5" 基于已被 runner 覆写的快照，已失效。）

### 8/7 那 3 条 carryover 行

`auto_213743_1635_1717` **不受本修复影响**：它今天这一轮的 BLOCK 原因是
`discovery.status=AUDITOR_UNAVAILABLE`——CPA `/responses` 在 `gpt-5.6-sol` 上
524 + 400、`gpt-5.5`/`gpt-5.4` 各 400，三模型全挂。`findings` 是空的，白名单不参与。
它那 3 条 `repaired=True` 会在任意一轮 CPA 健康时由 carryover 正常闭合。

附带收益：白名单加名后，`correction_carryover_consumed()`（经 `is_keep_current_disclosed`）
也能把"重放后被判为 keep-current"的 carryover 行算作**已消费**，
所以 `FINAL_REVIEW_CARRYOVER_UNCONSUMED` 那条死循环也更容易收敛。

### 全队（8/07–8/09 全部 18 份 BLOCK 终审回执）

- 53 条阻断 finding → **25 条**重分类为 disclosed；28 条仍拦。
- 16 份 `FLAGGED` 回执中 **9 份**不再抛 `FINAL_REVIEW_UNRESOLVED_FINDINGS`。
- 剩余 28 条的构成：`repaired=True` 系 17 条（属 carryover 群体 A）、
  `SKIPPED_BUDGET` 6 条、`UNCERTAIN` 系 4 条（选项 C）、`JUDGE_REJECTS_CLOSED_SET` 1 条。

关于简报转述的"8 条"：本次精确扫描（只认 `final-review-audit.v2` + `release_gate=BLOCK`）
的口径是 **30 条 `NOT_APPLIED` finding 因分支名未登记被拦**，其中 **25 条**符合严格 B 语义。
"8" 是另一个快照的数。

---

## 5. 测试

- 新增/改写 3 个测试，**修复前逐个实证失败**（`git stash` 掉 src 改动后跑，
  3 failed / 2 passed），修复后 5 passed。
- 反向门覆盖：`mutation_authority` 缺失 / `status=BLOCK` / `status=PASS`（授权已行使）/
  `repaired=True` / `timing_immutable=False` / `status=UNCERTAIN` / `status=UNAVAILABLE`
  / 未登记分支名 —— **全部仍必须拦住**；另有"把已改字节的行塞进
  `unresolved_findings_disclosed`"仍抛 `FINAL_REVIEW_FINDINGS_CONTRACT_INVALID`。
- 全量 `python3 -m pytest tests/ -q`：**3775 passed, 0 failed**（87.7s，仅 2 条
  pypinyin 的 `codecs.open` DeprecationWarning，与本次改动无关）。
  基线 3773 + 本次新增 2 个测试 = 3775，无回归、无 skip 掩盖。

---

## 6. 剩余风险

1. **选项 C 未决**（唯一还卡着 8/9 一个候选）：分流点注释说 UNCERTAIN + KEEP_CURRENT 可披露，
   合同判据要求 OBSERVED，两处矛盾；`HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY`
   整条路端到端是死的。需 Ivan 裁定。
2. **硬退出丢 carryover**：超时/无诊断退出/崩溃时 `persist_final_review_carryover` 不执行，
   B 已付费的声学裁定整轮丢失（8/8 实测 3 个候选中招）。
3. **CPA 供应商可用性**：`auto_213743_1635_1717` 今天死于三模型全 400/524。
   这条与字幕合同无关，是独立故障面。
4. **白名单-引擎绑定只保住了一个名字**：新加的那条用 import 绑死了，
   `JUDGE_KEEPS_CURRENT` 仍是字符串字面量。同类漂移仍可能再发生；
   彻底治法是把所有 decided-keep 分支名收敛成引擎侧的一个 frozenset 常量再被合同 import。
5. **本修复不改字节，也不保证发得出去**：解开的只是 `FINAL_REVIEW_UNRESOLVED_FINDINGS`
   这一个闸；真实重跑的 discovery 非确定性，边界门/封面/投稿门都还在后面。
6. **未部署**：按纪律本轮不 deploy。free 上的 runner 仍在跑旧代码。

---

## 7. 变更清单

- `src/autoslice/final_review_contract.py`
  - import 引擎常量 `WITNESS_CONFLICT_UNSUPPORTED_PROPOSED`；
  - `_DECIDED_KEEP_CURRENT_BRANCHES` 补入该常量；
  - 注释重写：引用已核验的 Ivan 原话与出处、写明收录三门槛、标注两个已死名字。
- `tests/test_final_review_contract.py`
  - 正向表补新分支；infra 负向表换掉 2 个幽灵名；
  - 新增 `test_witness_conflict_keep_current_is_a_decided_keep`（含 7 条反向门 + 白名单-引擎绑定断言）；
  - 新增 `test_release_accepts_disclosed_witness_conflict_and_still_blocks_unauthorized`（合同层放行 + 越权仍拦）。
