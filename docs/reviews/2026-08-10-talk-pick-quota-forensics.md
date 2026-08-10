# 切片数量上限裁定考据 — 2026-08-10

只读取证报告。回答 Ivan 2026-08-10 的三问：8/7 是否放宽到 10、10 从哪来、为什么 8/7 现在有 10 条。

**取证口径**：只认 Ivan 本人的 user turn（transcript `type=user` ∧ `origin.kind=human` ∧ 非 sidechain ∧ 非 tool_result）。
扫描面 = `~/.claude/projects/**/*.jsonl` 全量 10,460 条 user 记录（其中 human 1,883 条）+ `~/.codex/sessions/**/*.jsonl` 303 个 rollout。
助手转述、handoff 正文、subagent prompt 一律不作裁定依据；Ivan 粘贴的「助手起草、他转发」的 prompt 另行标注。

---

## 一、结论速览

1. **Ivan 的记忆是对的。** 他确实在 2026-08-07 说过放宽到 10 —— 逐字：「本场游戏直播的切片可突破5个上限，放宽到10个。当然，**前提是分数在90分以上**」。
2. **但 10 不是 8/7 现在有 9~10 条的原因。** 真正的因果杠杆是**分数门 90 → 85**：这四条额外切片得分 89.0 / 87.25 / 86.75 / 86.0，**在 90 门下一条都进不来**，在 85 门下四条全进。
3. 85 这个门来自 Ivan 8/8 的另一条裁定（「8.8切片配额到20条，分数在85分以上即可」），那条按日期限定给 8/8，却被实现成**游戏 lane 的全局常量**。而 8/8 根本不是游戏场（`session_game_context/2026-08-08.json` = `NO_MATCH`），所以那次改动**没有管到 8/8，反而回溯放宽了 8/7**（全库唯一 `RESOLVED` 的游戏日）。
4. 任务单假设的「判定口径不符（Ivan 说游戏场 / 流水线判 talk）」**不成立**：游戏 lane 根本不看 `scene_kind`。free 上 `session_game_context/2026-08-07.json` = `RESOLVED`（鹅鸭杀，17 个独立特征面 / 90 次命中），Ivan 和流水线在这点上一致。
5. 按 Ivan 8/7 的原话判：8/7 的合法交付数 = **5**（上限 10，但 6-10 号席位要 ≥90，全场没有第三条 ≥90）。现状 9 条在飞 ⇒ **超额 4 条**，且四条全部依赖一道 Ivan 没有授予 8/7 的门。定性 = **口径混乱导致的超额**，不是流水线失控（四条都过了一道真门，只是那道门是别人家的）。

---

## 二、Ivan 关于「切片数量上限」的全部逐字原话

全部为 `origin=human` 的 user turn。时间为 UTC；`session:line` 可直接定位 `~/.claude/projects/-Users-ivan-Project-vtuber-slice/<session>.jsonl`。

| # | UTC 时间 | 出处 | 逐字原话（节录到与配额相关的完整句） |
|---|---|---|---|
| R1 | 2026-07-10T00:43:08 | `1d417b3e:926` | 「歌切那个规则是至多切两个按弹幕量排序，如果直播没唱完整歌就不必强行歌切」（歌 lane，非 talk） |
| R2 | 2026-07-13T05:12:42 | `4444080d:860` | 「只是因为bilibili限制一天上传10个视频……如果不只限制10个最好，如果限制10个，可以暂缓歌切的上传」（**B 站每日投稿上限，与选题配额无关**） |
| R3 | 2026-07-14T15:03:28 | `4444080d:2513` | 「不是每场直播只做2个吗，为什么现在做10首？」（歌 lane） |
| R4 | 2026-07-19T23:28:37 | `4e3c1232:1117` | 「歌配额每场1」 |
| R5 | 2026-07-27T14:41:24 | `b533569f:3994` | 「为什么7.24和7.25会有10条，**标准流程不是只选5条吗？**」（**质询，非放宽**；确认 5 = 标准） |
| **R6** | **2026-08-07T21:54:29** | **`5cbe14f2:1026`** | 「……**以及本场游戏直播的切片可突破5个上限，放宽到10个。当然，前提是分数在90分以上。**……」 |
| R7 | 2026-08-08T05:58:05 | `5cbe14f2:1569` | 「理论上应该至少能出5个切片，因为流水线理论上足够强大强大到能通过每一个fail-closed门，并且**日常就应该有5个配额以及歌切**」 |
| R8 | 2026-08-08T19:37:51 | `5cbe14f2:1850` | 「……**为什么8.7的成品只有4条**，歌切呢？……」 |
| **R9** | **2026-08-08T19:48:40** | **`5cbe14f2:1869`** | 「……还有，**8.8切片配额到20条，分数在85分以上即可，候选也最好搞多一点。**」 |
| R10 | 2026-08-09T00:25:56 | `d975757c:1224` | 「正常产线因为现在被88占据所以也可以放到wsl，另外，**现在达不到5篇要求所以可以直接补上候补**。歌也是。我是建议87的所有都用wsl出。」（针对 8/7，指向补满基础 5 席） |
| R11 | 2026-08-09T05:22:08 | `d975757c:2090` | 「另外**88不是配额放宽了吗，怎么只有6条**」 |
| **R12** | **2026-08-09T05:38:56** | **`d975757c:2102`** | 「**我记得我当时说过88这个3D live场放宽到15个，然后当天的杂谈场认为是独立的，自然有5个。**……」 |
| R13 | 2026-08-10T00:39:39 | `a4ce1306:1333` | 「……还有就是**8月7号的那个，我放宽到了多少个切片来着？**……」 |
| R14 | 2026-08-10T01:03:42 | `a4ce1306:1429` | 「……**8/7既然只允许5，为什么会跑到7，8个切片？我其实记得我放宽到10了**，你查查我们对话记录。……」 |

**未找到**：除 R6 外，没有任何 Ivan 本人 turn 把 10 用于 8/7 之外的范围；没有任何 Ivan turn 把 10 定为全局或 talk 场默认；Codex 侧 303 个 rollout 中关于配额的命中全部是**助手起草的 worker prompt 引用 F13**，无 Ivan 新增裁定。

**转述件（不作裁定依据，仅标注）**：`f95a556a:3` 与 `502e228c:3`（2026-08-09T06:12–06:13，Ivan 粘贴的接棒 prompt）内含「F13(事件场配额15+同日场独立5)」字样 —— 该措辞由上一个助手起草、Ivan 转发，其**权威来自 R12 本身**，不构成独立裁定。（前车之鉴：7/25「截图优先」即助手措辞被硬化成假裁定。）

### R6 的完整上下文（证明「本场」= 2026-08-07）

同一条消息末尾 Ivan 写：「我在 `/Users/ivan/Project/vtuber-slice/lidousha/2026-08-07/刚发誓再也不信真善美，她转头就自夸最.speaker.srt` 里做了改动」。
同会话开场（`5cbe14f2:3`，2026-08-07T18:28:14）他已定性该场：「**这个直播是在进行鹅鸭杀**……第三，这是多人直播场景」。
⇒ R6 的「本场游戏直播」明确指 2026-08-07 场，无歧义。

---

## 三、10 的落地路径与它后来怎么变成 20/85

| 时间(UTC) | 事件 | 结果 |
|---|---|---|
| 2026-08-07T21:54 | **R6**：游戏场 5→10，额外席位 ≥90 | 裁定 |
| 2026-08-07T22:18 | `f72c07e` *feat(autoslice): game-session talk-pick cap 5→10 gated at score>=90* | `GAME_SESSION_TALK_PICK_CAP = 10`、`GAME_SESSION_EXTRA_SLOT_MIN_SCORE = 90.0`，条件 = 该日 `session_game_context` 为 `RESOLVED` |
| 2026-08-08T19:48 | **R9**：8.8 配额 20 条 / ≥85 | 裁定（**按日期限定**） |
| 2026-08-08T20:06 | `4af4a88`（commit message 自述：「Ivan 2026-08-08「8.8切片配额到20条，分数在85分以上即可，候选也最好搞多一点」: `GAME_SESSION_TALK_PICK_CAP` 10->20, `GAME_SESSION_EXTRA_SLOT_MIN_SCORE` 90->85」） | **把按日裁定写进了游戏 lane 全局常量**；同时 `TALK_ATTEMPT_CAP` 10→20、`PER_SEGMENT_CANDIDATES` 12→18 |
| 2026-08-09T05:38 | **R12**：88 3D live 场 15，同日杂谈场独立 5 | 裁定（Ivan 自述为**回忆**） |
| 2026-08-09T09:31 | `6faf1fd` F13/F14 | 新增独立事件 lane `EVENT_TALK_PICK_CAP = 15` / `EVENT_EXTRA_SLOT_MIN_SCORE = 85.0` |

### 两处口径断裂

**断裂 A（本案主因）：R9 的射程被放大。**
R9 说的是「8.8」。而 8/8 在流水线里**不是游戏场**：free 上 `state/session_game_context/2026-08-08.json` 的 `status` = `NO_MATCH`。
所以 `4af4a88` 改游戏 lane 常量这件事：
- 对 8/8 **完全无效**（8/8 的 `session_game_context` 是 `NO_MATCH`，游戏 lane 根本不触发；8/8 当时走 `default_cap=5`，直到 8/9 的 F13 才拿到事件 lane）；
  实测 8/8（free `state/2026-08-08.json`，单会话 `live-20260808Tunknown`）：`segment_scene_contexts` 7 段中 5 段 `scene_kind=event`、2 段 `talk`，picks 5 条里 4 event / 1 talk。⇒ 现行代码下 8/8 的 event 段走 `event:<session>` 15/85、talk 段走独立的 `talk:<session>` 5 —— 与 R12「同日的杂谈场认为是独立的，自然有5个」一致；

- 对 8/7 **完全有效且是回溯的** —— 8/7 是全库唯一 `RESOLVED` 的游戏日，它的 cap 与门在下一个 tick 就从 10/90 变成 20/85。

配额在每个 tick 由常量**实时重算**（`talk_pick_cap()` 每次读常量 + state 文件），没有任何「按准入时冻结」的记录，所以一次常量修改会直接改写一个已经裁定过的日子的合法性。这正是 memory `lidousha-publication-registry-hold-gate` 里「冻结置换恢复不做 live 政策重算」那条教训的同型病。

**断裂 B（待 Ivan 再裁）：15 还是 20/85。**
R9（8/8 当场）= 20 条 / ≥85；R12（8/9 回忆）= 15。两条都是 Ivan 本人 turn，R12 更晚因而在时序上覆盖 R9，代码按 R12 实现了 `EVENT_TALK_PICK_CAP = 15`。但 R12 自述「**我记得我当时说过**」，即它是一次回忆而非新裁定 —— 与本次调查的触发原因（R14「我其实记得我放宽到10了」）是同一个模式。**本报告不代为选择**，列出供 Ivan 复裁。

### 顺带澄清三个不同的「10」，避免再被混为一谈

- **R6 的 10** = 游戏场 talk pick 上限（本案）。
- **R2 的 10** = B 站每日投稿条数上限（平台限制）。
- **R5 的 10** = 7/24、7/25 各自出现 10 条时 Ivan 的**质询**，其立场是「标准流程只选 5 条」。

---

## 四、8/7 现在为什么有 10 条：逐条准入路径

### 现场事实（free，只读）

- 部署提交 `af35e5e9`（2026-08-09T23:55Z 部署），`repo/src/autoslice/game_context.py`：`GAME_SESSION_TALK_PICK_CAP = 20`、`GAME_SESSION_EXTRA_SLOT_MIN_SCORE = 85.0`；`repo/scripts/free_session_autoslice.py`：`MAX_TALK_PICKS = 5`、`TALK_ATTEMPT_CAP = 20`。**即：admit 这四条的就是当前生产版本本身。**
- `state/session_game_context/2026-08-07.json` = `RESOLVED`（鹅鸭杀 / Goose Goose Duck；`distinct_detection_surfaces=17`，`total_detection_hits=90`）。
- `state/2026-08-07.json`：`recording_sessions = ["live-20260807Tunknown"]`（**全天单会话**），`picks=6`，`pending_talk=4`，`talk_backlog=20`，`not_selected=51`。

### 实测准入账（把 free 的 state 拷贝到本地、用仓库真函数复算；`publication_row_is_verified` 依赖的三份 authority 文件已在 free 上逐一核对存在且 sha256/bytes 匹配）

```
policy      = TalkQuotaPolicy(kind='game', scope_key='game:live-20260807Tunknown',
                              cap=20, extra_slot_min_score=85.0, recording_date='2026-08-07')
produced             = 5      # 3 published(publication_row_is_verified) + 1 media_ready_cover_pending + 1 review_ready
reserved_for_revival = 0      # 没有 status=='failed' ∧ failure_recoverable 的记录
attempts_left        = 20 - 6 = 14
slots                = min(20 - 5 - 0, 14) = 15
```

### 逐条账（10 条 = 6 picks + 4 pending）

| 记录 | candidate_id / 片段 | status | effective_score | 席位 | 准入路径 |
|---|---|---|---|---|---|
| P5 | `auto_200736_298_383` | published `BV1JLuj6zEdM` | 99.0 | 基础席 1-5 | 无分数门（≤ `MAX_TALK_PICKS`） |
| P2 | `auto_223750_913_1322` | media_ready_cover_pending | 99.0 | 基础席 1-5 | 无分数门 |
| P3 | `auto_210739_1142_1436` | review_ready | 86.5 | 基础席 1-5 | 无分数门 |
| P1 | `auto_203735_555_680` | published `BV1houS6SEF3` | **83.25** | 基础席 1-5 | 无分数门（**低于 85，任何额外席位门都进不来**） |
| P4 | `auto_220747_488_680` | published `BV1hfuS6EENb` | **82.25** | 基础席 1-5 | 无分数门（同上） |
| P6 | `auto_220747_1271_1323` | candidate_rejected | 88.25 | **不占席** | 曾 revive（`expected_fix_commit=26dfd83`，`story_contract`）后再拒；`candidate_rejected` 不在 `DELIVERED_TALK_STATUSES`、不是 publication-verified、也不是 `failed`+recoverable ⇒ 只吃掉 20 次 attempt 中的 1 次，不吃配额 |
| T1 | 片段 `21-37-43` | pending | 89.0 | **额外席 6** | `sanctioned_candidate_revival`（原因 `subtitle_authority`，`expected_fix_commit=702aa002…`），过 ≥85 门 |
| T2 | 片段 `21-37-43` | pending | 87.25 | **额外席 7** | 同上 revive，过 ≥85 门 |
| T3 | 片段 `22-07-47` | pending | 86.75 | **额外席 8** | 同上 revive，过 ≥85 门 |
| T4 | 片段 `20-37-35` | pending | 86.0 | **额外席 9** | `final_review_carryover`，过 ≥85 门 |

**复活件占不占席位：占。** `_admits_extra_slot()` 对 revive 没有豁免 —— revive 回到 `pending_talk` 后与普通候选同排序、同走额外席位分数门。代码里另有一个 `reserved_for_revival`，那是给 `status=='failed'` ∧ `failure_recoverable` 的 picks **预留**席位（防止成功的兄弟把可恢复失败挤掉），8/7 该值为 0，与本案无关。

**席位序号怎么算**：`position = produced + reserved_for_revival + len(session_keep) + 1`。8/7 的 `produced` 已经是 5，所以**任何新 keep 的第一条起步就是第 6 位**，必过分数门。四条 pending 依次拿 6/7/8/9。

### 反事实：如果按 Ivan 8/7 的原话（cap 10、门 90）

全场（picks + pending + `talk_backlog` 20 条 + `talk_below_confidence_threshold` 3 条）的 effective_score 分布：

```
99.0, 99.0, 89.0, 88.25, 87.25, 86.75, 86.5, 86.0, 83.25, 82.25   (in-flight 10 条)
backlog 最高 82.75，其余 ≤82.25；below-threshold ≤66.5
```

- ≥90 的候选**全场只有 2 条**（两条 99.0），而它们已经在基础席 1-5 里。
- 6-10 号席位在 90 门下 **无人合格** ⇒ 8/7 在 Ivan 原话下的合法交付数 = **5**。
- 换成 85 门，≥85 的有 8 条 ⇒ 额外席位放进 4 条 ⇒ 今天的 9 条。

**所以 Ivan 问的「为什么会跑到 7、8 个」，答案不是 cap（10 也好 5 也好都不是直接原因），而是那道 90→85 的门。**

---

## 五、结论与最小落地改法（仅设计，未实现）

### 定性

- **8/7 的合法上限（按 Ivan 唯一相关裁定 R6）= 10 席，其中 6-10 席要 ≥90 分。**
- **8/7 的实际合法交付数 = 5**（没有第三条 ≥90）。
- **现状 = 超额 4 条 + 口径混乱**：四条额外件全部合规地通过了 85 门，但 85 这道门对 8/7 **没有 Ivan 授权**，它是 8/8 的按日裁定被写成 lane 全局常量后回溯溅到 8/7 的。
- 需要说明的**减责事实**：R8（「为什么8.7的成品只有4条」）和 R10（「现在达不到5篇要求所以可以直接补上候补」）说明 Ivan 当时在**主动催多**。但 R10 的射程是补满基础 5 席，不覆盖 6 席以上。

### 最小改法（四件，按依赖排序）

1. **把游戏 lane 常量退回 Ivan 原话。**
   `src/autoslice/game_context.py:245-246`：`GAME_SESSION_TALK_PICK_CAP = 20 → 10`、`GAME_SESSION_EXTRA_SLOT_MIN_SCORE = 85.0 → 90.0`。
   这是全文唯一有 Ivan 逐字授权的取值。
   **危险点：这条单独做会立刻误伤。** cap/门每 tick 实时重算，退回 90 门后下一个 tick 就把 T1-T4 从 `pending_talk` 打回 `talk_backlog`（其中三条还是已经 revive 过的）。所以第 1 件必须和第 3、4 件一起上，或先由 Ivan 决定第 4 件。

2. **给「按日/按场裁定」一个正式承载面，别再改 lane 常量。**
   新增 `state/talk_quota_overrides/<recording_date>.json`，schema 至少含 `{cap, extra_slot_min_score, source_quote, ruled_at, ruled_by}`；在 `resolve_talk_quota_policy()` 的优先级链**最前面**读取（override → 游戏 → 事件 → 普通 talk）。
   fail-closed：文件缺失/不可读/schema 不合 ⇒ 直接落回 lane 默认，绝不放宽。
   R9 的「8.8 → 20/85」就写成 `2026-08-08.json` 一行，任何其他日期都不再继承。

3. **给每条 pick 冻结准入时的政策。**
   在 pick 记录上写 `talk_quota_policy_at_admission = {kind, scope_key, cap, extra_slot_min_score, policy_source, admitted_position}`。
   已准入的席位以冻结值为准，后续常量收紧/放宽不再回溯重裁一个已闭合的日子。这同时给本次这类考据留下自证链（现在 state 里查不到任何一条「我是在哪套政策下被 admit 的」）。

4. **8/7 那 4 条的处置，交 Ivan 定。** 二选一，不由实现方决定：
   (a) 追认 —— 给 8/7 补一条 override（cap 10 / 门 85），把四条就地合法化；注意 cap 10 下 5+4=9 仍在界内。
   (b) 退回 —— 四条打回 `talk_backlog`，8/7 定格 5 条。

5. **（附带）请 Ivan 复裁 8/8 事件场是 15 还是 20/85**（断裂 B）。当前代码取 15，依据是 R12 这次**回忆**；R9 的当场原话是 20/85。

---

## 附：证据可复现路径

- 转写取证：`~/.claude/projects/**/*.jsonl`，筛 `type=user ∧ origin.kind=human ∧ ¬isSidechain ∧ ¬toolUseResult`。表二每行的 `session:line` 可直接定位。
- 代码：`src/autoslice/game_context.py:239-277`（常量与 `talk_pick_cap`）、`src/autoslice/talk_quota_policy.py:17-18,72-118`（lane 优先级）、`src/autoslice/candidate_selection.py:261-296`（准入账）与 `:930-985`（`_admits_extra_slot` 与排序）、`scripts/free_session_autoslice.py:272,277,332,333`。
- 提交：`f72c07e`（2026-08-07T22:18Z，10/90）、`4af4a88`（2026-08-08T20:06Z，20/85）、`6faf1fd`（2026-08-09T09:31Z，事件 lane 15/85）。
- free（只读）：`/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` = `af35e5e9…`；`state/session_game_context/2026-08-07.json`（RESOLVED）与 `2026-08-08.json`（NO_MATCH）；`state/2026-08-07.json`。
- 准入复算：把上述两个 state 文件拷到临时树，`import scripts.free_session_autoslice` 后置 `runner.BASE`，调用 `candidate_selection._talk_admission_for_policy()`。注意 `publication_row_is_verified()` 会校验 authority 文件的实际 sha256/bytes，**在 Mac 上必然假阴性**（文件在 free 的 `out/` 下），三条 published 的 authority 已在 free 上单独核对通过。
